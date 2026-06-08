"""Supervisor cost-attribution helper (Task S8, §A11-E1).

THE PROBLEM (E1, the highest-risk integration point). The worker's per-step
cost loop (``executor/graph.py``) is gated ``if "agent" in event`` — and
``"agent"`` is the *ReAct* LLM node key. The Supervisor topology's LLM-bearing
nodes (``scope`` / ``supervisor`` / ``writer`` / verify) and its fan-out
sub-agents emit under OTHER node keys, so the existing loop never records their
spend. Without a fix a Deep Research run records ~$0 and never trips the budget
pause — the most expensive topology runs unmetered.

THE MECHANISM (verified against ``langgraph==1.0.5``, 2026-06-08 spike). Spike #6
in plan §A11 proposed ``subgraphs=True`` namespacing, but that assumed the
sub-agents are reached as ``Send``-subgraph-NODES of the streamed graph. The
shipped ``run_subagent`` (``executor/subagents/fanout.py``) runs each sub-agent
via a NESTED ``compiled.ainvoke(...)`` inside the fan-out node — and a throwaway
spike confirmed the outer ``astream(stream_mode="updates", subgraphs=True)`` does
NOT descend into a nested ``ainvoke`` (the sub-agent never appears as a namespace
event; ``stream_mode="updates"`` also only yields a node's RETURN value, not its
internal LLM messages). So the working approach is the spec's option (b):
**helper-accumulated return**. Every LLM-bearing Supervisor node accumulates its
own ``usage_metadata`` into the ``step_usage`` state channel it returns;
``run_subagent`` surfaces its nested sub-agents' accumulated usage via
``SubagentResult.usage`` and the fan-out node folds it into ``step_usage`` too.
``stream_mode="updates"`` then yields one event per node (and one per fan-out
``Send`` branch), each carrying its ``step_usage`` delta — which the parent cost
loop sums and writes ADDITIVELY to the parent's super-step ``checkpoint_id``.

PATTERN A. The accumulated usage is the PARENT task's spend — no ``sub_agent_id``
column, no per-sub-agent ledger rows, no per-tree rollup, no refund path. The
``step_usage`` dict carries the standard LangChain ``usage_metadata`` numeric
keys so the existing provider-aware ``_calculate_step_cost`` extractor consumes
it unchanged (no provider branches added here — §LLM Provider Support).
"""

from __future__ import annotations

from typing import Any

# Re-export the pure usage helpers from the leaf module so existing imports of
# ``executor.supervisor.cost`` keep working. ``merge_step_usage`` is the
# supervisor-facing name for the ``step_usage`` channel reducer (== merge_usage).
from executor.usage import (  # noqa: F401
    merge_usage as merge_step_usage,
    usage_from_message,
    usage_from_messages,
)


class UsageAccumulatingModel:
    """Wrap a chat model so every ``ainvoke`` call's usage is accumulated.

    The Writer node's verify pass (``citations.verify``) makes one LLM call per
    cited finding and returns only the support flags — discarding the call usage.
    Rather than reshape S7's ``citations.verify`` return contract, ``writer_node``
    passes a model wrapped in this proxy as the verify model; the wrapper sums
    every verify call's ``usage_metadata`` into :attr:`usage`, which the node then
    folds into its ``step_usage`` return so the verify spend is billed too (E1).

    Only ``ainvoke`` is intercepted (the single entry point the supervisor nodes
    + ``citations.verify`` use). Any other attribute access proxies to the
    wrapped model, so it is a drop-in for the duration of the call.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.usage: dict[str, int] = {}

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._inner.ainvoke(*args, **kwargs)
        self.usage = merge_step_usage(self.usage, usage_from_message(response))
        return response

    def __getattr__(self, name: str) -> Any:
        # Proxy everything else (e.g. bind_tools) to the wrapped model. Only
        # reached for attributes not set on the wrapper itself.
        return getattr(self._inner, name)
