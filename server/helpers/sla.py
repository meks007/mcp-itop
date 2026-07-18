"""
helpers/sla.py

SLA field list and pass/breach classification helpers.
No iTop requests, no SQLite.
"""

from __future__ import annotations

SLA_ANALYSIS_FIELDS = (
    "id,ref,title,status,service_name,org_name,agent_name,caller_name,"
    "start_date,assignment_date,resolution_date,close_date,"
    "sla_tto_passed,sla_ttr_passed,time_spent,"
    "last_update"
)

_SLA_PASSED_VALUES = {"true", "yes", "1"}
_SLA_BREACHED_VALUES = {"false", "no", "0"}


def sla_is_passed(val: str) -> bool:
    """Return True when the SLA value indicates the SLA was met."""
    return val.strip().lower() in _SLA_PASSED_VALUES if val else False


def sla_is_breached(val: str) -> bool:
    """Return True when the SLA value indicates the SLA was breached."""
    return val.strip().lower() in _SLA_BREACHED_VALUES if val else False
