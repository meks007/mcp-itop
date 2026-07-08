"""
Analytics tools: SLA, workload, idle agents, service/caller quality, summary.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from helpers import (
    SLA_ANALYSIS_FIELDS,
    extract_objects,
    format_duration,
    format_table,
    parse_date_range,
    sla_is_breached,
    sla_is_passed,
    str_or,
)


def register(mcp, itop_request):
    """Register all analytics tools on the given mcp instance."""

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
        s, e = parse_date_range(start_date, end_date)

        oql = f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'"
        if service_name:
            oql += f" AND service_name = '{service_name}'"

        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": oql,
            "output_fields": SLA_ANALYSIS_FIELDS,
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return (
                f"No tickets found for period {s[:10]} to {e[:10]}."
                + (f" Service: {service_name}" if service_name else "")
            )

        total = len(tickets)
        tto_passed = tto_breached = tto_na = 0
        ttr_passed = ttr_breached = ttr_na = 0
        resolution_times = []
        spent_times = []

        for t in tickets:
            f = t["fields"]
            # TTO
            tto = str_or(f, "sla_tto_passed")
            if sla_is_passed(tto):
                tto_passed += 1
            elif sla_is_breached(tto):
                tto_breached += 1
            else:
                tto_na += 1
            # TTR
            ttr = str_or(f, "sla_ttr_passed")
            if sla_is_passed(ttr):
                ttr_passed += 1
            elif sla_is_breached(ttr):
                ttr_breached += 1
            else:
                ttr_na += 1

            # Resolution time
            st = f.get("start_date")
            rt = f.get("resolution_date")
            if st and rt:
                try:
                    diff = (
                        datetime.fromisoformat(rt) - datetime.fromisoformat(st)
                    ).total_seconds()
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
                f"  Median: {format_duration(median)}",
                f"  Min:    {format_duration(resolution_times[0])}",
                f"  Max:    {format_duration(resolution_times[-1])}",
            ])

        if spent_times:
            avg_spent = sum(spent_times) / len(spent_times)
            lines.append(f"  Avg time_spent: {format_duration(avg_spent)}")

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
        s, e = parse_date_range(start_date, end_date)

        oql = f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'"
        if team_name:
            oql += f" AND team_name = '{team_name}'"
        if agent_name:
            oql += f" AND agent_name = '{agent_name}'"

        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": oql,
            "output_fields": "id,ref,title,status,agent_name,team_name,time_spent,start_date,resolution_date",
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return "No tickets found for the specified filters."

        agents: dict[str, dict] = {}
        for t in tickets:
            f = t["fields"]
            agent = str_or(f, "agent_name") or "(unassigned)"
            if agent not in agents:
                agents[agent] = {
                    "closed": 0,
                    "open": 0,
                    "total_time": 0.0,
                    "team": str_or(f, "team_name"),
                    "tickets_open": [],
                }
            a = agents[agent]
            status = str_or(f, "status")
            if status in ("closed", "resolved"):
                a["closed"] += 1
            else:
                a["open"] += 1
                a["tickets_open"].append(str_or(f, "ref"))

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
                format_duration(data["total_time"]),
                backlog + flag,
            ])

        lines = [f"**Agent Workload** ({s[:10]} - {e[:10]})", ""]
        lines.append(format_table(header, rows))
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
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        oql = f"SELECT UserRequest WHERE status='{status}' AND assignment_date < '{cutoff}'"
        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": oql,
            "output_fields": "id,ref,title,agent_name,team_name,assignment_date,last_update,start_date",
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return f"No idle tickets found (status={status}, idle >{hours}h)."

        idle_list = []
        for t in tickets:
            f = t["fields"]
            agent = str_or(f, "agent_name") or "?"
            last_upd = f.get("last_update")

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
                    "ref": str_or(f, "ref"),
                    "title": str_or(f, "title"),
                    "agent": agent,
                    "team": str_or(f, "team_name") or "-",
                    "assigned": str_or(f, "assignment_date", "?")[:16],
                    "last_update": str_or(f, "last_update", "?")[:16],
                })

        if not idle_list:
            return "No truly idle agents found - all tickets have recent activity."

        header = ["Ticket", "Title", "Agent", "Team", "Assigned", "Last Action"]
        rows = []
        for t in idle_list[:30]:
            rows.append([
                t["ref"],
                t["title"][:40],
                t["agent"],
                t["team"],
                t["assigned"],
                t["last_update"],
            ])

        remaining = len(idle_list) - 30
        out = [f"**Idle Agents** (>{hours}h without action, status={status})", ""]
        out.append(f"Found {len(idle_list)} idle tickets:")
        out.append("")
        out.append(format_table(header, rows))
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
        import re

        s = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        e = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'",
            "output_fields": "id,ref,title,service_name,service_id,caller_name,agent_name,status",
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return "No tickets found."

        STOP_WORDS = {"the", "a", "an", "of", "in", "to", "for", "with", "on", "at", "is", "it", "as"}

        def extract_keywords(title: str) -> set:
            words = set()
            for w in re.findall(r'\w{3,}', title.lower()):
                if w not in STOP_WORDS:
                    words.add(w)
            return words

        ticket_kw = []
        for t in tickets:
            title = str_or(t["fields"], "title", "")
            kw = extract_keywords(title)
            ticket_kw.append((t, kw))

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
                if len(kw1 & kw2) >= 2:
                    group.append(j)
                    used.add(j)
            if len(group) >= min_similar:
                services = set()
                for idx in group:
                    services.add(str_or(tickets[idx]["fields"], "service_name", "(none)"))
                if len(services) > 1:
                    groups.append(group)

        if not groups:
            return "No significant service mismatches found."

        out_lines = [f"**Service Quality Check** (last {days} days)", ""]
        for g in groups:
            kw_sample = ticket_kw[g[0]][1]
            kw_str = ", ".join(sorted(kw_sample)[:5])
            out_lines.append(f"**Similar tickets (keywords: {kw_str})**")
            header = ["Ticket", "Caller", "Service", "Agent", "Status"]
            rows = []
            for idx in g:
                t = tickets[idx]
                f = t["fields"]
                rows.append([
                    str_or(f, "ref"),
                    str_or(f, "caller_name", "?"),
                    str_or(f, "service_name", "(none)"),
                    str_or(f, "agent_name", "-"),
                    str_or(f, "status"),
                ])
            out_lines.append(format_table(header, rows))
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

        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'",
            "output_fields": "id,ref,title,service_name,caller_name,agent_name,status,start_date",
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return "No tickets found."

        callers: dict[str, dict] = {}
        for t in tickets:
            f = t["fields"]
            caller = str_or(f, "caller_name") or "(unknown)"
            if caller not in callers:
                callers[caller] = {"total": 0, "services": Counter()}
            callers[caller]["total"] += 1
            callers[caller]["services"][str_or(f, "service_name", "(none)")] += 1

        rows = []
        for caller, data in sorted(callers.items()):
            if data["total"] < min_tickets:
                continue
            most_common = data["services"].most_common(1)
            primary_svc, primary_count = most_common[0] if most_common else ("-", 0)
            primary_rate = primary_count / data["total"] * 100
            total_services = len(data["services"])

            if primary_rate < 50:
                flag = "systematically wrong"
            elif primary_rate < 70:
                flag = "often wrong"
            elif primary_rate < 90:
                flag = "sometimes wrong"
            else:
                flag = "OK"

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
        out = [
            f"**Caller Service Selection Quality** (last {days} days, >={min_tickets} tickets)",
            "",
        ]
        out.append(format_table(header, rows))
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

        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'",
            "output_fields": "id,ref,title,service_name,service_id,caller_name,agent_name,status",
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return "No tickets found."

        agents: dict[str, dict] = {}
        for t in tickets:
            f = t["fields"]
            agent = str_or(f, "agent_name") or "(unassigned)"
            if agent not in agents:
                agents[agent] = {"total": 0, "services": Counter(), "callers": set()}
            agents[agent]["total"] += 1
            agents[agent]["services"][str_or(f, "service_name", "(none)")] += 1
            agents[agent]["callers"].add(str_or(f, "caller_name", "?"))

        rows = []
        for agent, data in sorted(agents.items()):
            if data["total"] < min_tickets:
                continue
            most_common = data["services"].most_common(1)
            primary_count = most_common[0][1] if most_common else 0
            primary_rate = primary_count / data["total"] * 100
            total_services = len(data["services"])

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
        out.append(format_table(header, rows))
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

        result = await itop_request({
            "operation": "core/get",
            "class": "UserRequest",
            "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date <= '{e}'",
            "output_fields": SLA_ANALYSIS_FIELDS,
            "limit": str(limit),
        })

        tickets = extract_objects(result)
        if not tickets:
            return "No tickets found."

        total = len(tickets)
        by_status = Counter()
        sla_breaches = 0
        resolution_times = []

        for t in tickets:
            f = t["fields"]
            by_status[str_or(f, "status", "?")] += 1

            if sla_is_breached(str_or(f, "sla_tto_passed")) or sla_is_breached(
                str_or(f, "sla_ttr_passed")
            ):
                sla_breaches += 1

            st = f.get("start_date")
            rt = f.get("resolution_date")
            if st and rt:
                try:
                    diff = (
                        datetime.fromisoformat(rt) - datetime.fromisoformat(st)
                    ).total_seconds()
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
                f"Avg resolution: {format_duration(avg_res)}",
                f"Median resolution: {format_duration(median)}",
            ])

        return "\n".join(lines)
