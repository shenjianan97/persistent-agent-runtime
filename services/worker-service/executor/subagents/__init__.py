"""In-process sub-agent fan-out (Agent Modes — Supervisor Topology, Pattern A).

This package holds the **single shared fan-out primitive** that both delegation
drivers route through:

* Topology 1 — the ``dispatch_subagent`` tool (Task S4): a post-agent routing
  edge intercepts the LLM's tool call and ``Send``s it to the shared subagent
  node.
* Topology 2 — the Supervisor graph (Task S6): the graph structurally ``Send``s
  N sub-agents from the Supervisor's subtask list.

Both reach the **same** compiled ReAct subgraph node — built once here — so
neither driver forks a second copy. See
``docs/design-docs/agent-modes/design.md`` (*Shared fan-out machinery*,
*Execution model: in-process fan-out (Pattern A)*).
"""

from __future__ import annotations

from executor.subagents.fanout import (
    MAX_SUBAGENT_DEPTH,
    SUBAGENT_HEARTBEAT_EVENT,
    SubagentCeiling,
    SubagentResult,
    build_subagent_node,
    filter_headless_tools,
    run_subagent,
)

__all__ = [
    "MAX_SUBAGENT_DEPTH",
    "SUBAGENT_HEARTBEAT_EVENT",
    "SubagentCeiling",
    "SubagentResult",
    "build_subagent_node",
    "filter_headless_tools",
    "run_subagent",
]
