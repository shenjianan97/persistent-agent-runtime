"""Supervisor ("Deep Research") topology package.

A fixed four-phase LangGraph: Scope → Supervisor(+iteration) → parallel
Subagents → Writer. S5 lands phase 1 (``scope_node``) and the
``SupervisorState`` superset the rest of the graph reads/writes. S6/S7/S8 append
the remaining nodes, the keyed-results merge, the prompt templates, and the
graph compile — all additive over what is exported here.
"""

from __future__ import annotations

from executor.supervisor.nodes import (
    DECISION_CONTINUE,
    DECISION_STOP,
    WRITER_FINDINGS_CAP,
    SupervisorAllFailedError,
    parse_findings,
    reduce_findings,
    scope_node,
    supervisor_node,
    writer_node,
)
from executor.supervisor.state import SupervisorState, _merge_subagent_results

__all__ = [
    "SupervisorState",
    "scope_node",
    "supervisor_node",
    "writer_node",
    "parse_findings",
    "reduce_findings",
    "WRITER_FINDINGS_CAP",
    "SupervisorAllFailedError",
    "DECISION_CONTINUE",
    "DECISION_STOP",
    "_merge_subagent_results",
]
