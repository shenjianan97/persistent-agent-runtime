"""LLM-facing ``dispatch_subagent`` tool (Agent Modes — Supervisor Topology, S4).

``dispatch_subagent`` is the **LLM-emergent driver** over the shared in-process
fan-out helper (Task S3's ``executor.subagents.run_subagent``). In a ReAct loop
the model emits a ``dispatch_subagent`` tool call when it wants to delegate a
focused subtask without polluting its own context (e.g. *"investigate why test
X is flaky"*).

**This module declares the LLM-facing schema ONLY.** The tool's presence lets
the model *emit* the call; the actual delegation is **not** run inside the
ToolNode. A post-agent routing edge in :mod:`executor.graph` intercepts each
emitted ``dispatch_subagent`` call and ``Send``s it to the shared subagent node
(S3), which threads the sub-agent's summary back as a ``ToolMessage`` keyed to
the original ``tool_call_id`` while its internal working messages stay on a
separate channel (context isolation). See the design doc
(*Why ``dispatch_subagent`` routes through ``Send`` rather than the ToolNode*)
and the S4 task spec.

Because the tool body is never executed (the routing edge always intercepts the
call before the ToolNode runs), :func:`_dispatch_subagent_unreachable` exists
only to satisfy ``StructuredTool``'s coroutine requirement; reaching it would be
a wiring bug and it says so loudly.

The arg→ceiling mapping (``budget`` → :class:`SubagentCeiling`) lives here so
the routing edge stays a thin transport: ``budget`` is the per-sub-agent **turn
budget** (the knob the model reasons about — *how many steps may this sub-agent
take*), clamped to ``[MIN_SUBAGENT_TURN_BUDGET, MAX_SUBAGENT_TURN_BUDGET]`` and
paired with the platform-default token ceiling. ``budget`` bounds *cost*; the
structural depth cap (enforced in S3) bounds *nesting* — they are orthogonal.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from langchain_core.tools import StructuredTool

from executor.subagents import SubagentCeiling

DISPATCH_SUBAGENT_TOOL_NAME = "dispatch_subagent"

# ``budget`` clamp bounds (turn budget). The turn budget is the binding cost
# guard the LLM controls; the token ceiling is a backstop so runaway context
# growth cannot outspend the granted turns. The backstop scales PER TURN: the
# sub-agent meter accumulates each call's TOTAL tokens, re-billing the growing
# transcript every turn (~20k/turn observed for web research — task 223b155c),
# so a fixed 200k backstop silently re-bound at ~turn 10 and made any larger
# turn budget unreachable. 50k/turn keeps it a backstop, not the binding guard.
MIN_SUBAGENT_TURN_BUDGET = 1
MAX_SUBAGENT_TURN_BUDGET = 30
DEFAULT_SUBAGENT_TURN_BUDGET = 8
SUBAGENT_TOKEN_CEILING_PER_TURN = 50_000

DISPATCH_SUBAGENT_DESCRIPTION = (
    "Delegate a focused subtask to a fresh sub-agent that runs in its own "
    "isolated context window and returns only a distilled summary — keeping "
    "your own context clean. Use it for a self-contained investigation or "
    "research thread (e.g. 'find out why test X is flaky') whose intermediate "
    "steps you do not need to see. The sub-agent is headless (it cannot ask a "
    "human anything) and runs to a bounded budget; you receive a single "
    "summary back. Pass `tools` as the subset of YOUR tools the sub-agent may "
    "use (it can never use a tool you lack), and `budget` as the maximum number "
    "of reasoning/tool steps it may take."
)


class DispatchSubagentArguments(BaseModel):
    """LLM-facing args for ``dispatch_subagent``.

    ``depth`` is deliberately ABSENT — nesting depth is sourced from the parent
    graph state by the routing edge, never from the model, so a crafted call
    cannot escalate past ``MAX_SUBAGENT_DEPTH``.
    """

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description=(
            "The full instruction for the sub-agent — a self-contained subtask. "
            "The sub-agent sees only this prompt, none of your conversation."
        ),
    )
    tools: list[str] = Field(
        default_factory=list,
        description=(
            "Names of the tools the sub-agent may use. Must be a subset of the "
            "tools you yourself have; tools you lack are ignored. Leave empty "
            "for a reasoning-only sub-agent."
        ),
    )
    budget: int = Field(
        default=DEFAULT_SUBAGENT_TURN_BUDGET,
        ge=MIN_SUBAGENT_TURN_BUDGET,
        description=(
            "Maximum number of reasoning/tool steps the sub-agent may take "
            "before it must return. Bounds cost; clamped to a platform maximum."
        ),
    )


def budget_to_ceiling(budget: int | None) -> SubagentCeiling:
    """Map the LLM-facing ``budget`` (turn budget) to a :class:`SubagentCeiling`.

    ``budget`` is the turn cap, clamped to ``[MIN, MAX]``; the token ceiling
    is the per-turn backstop scaled by the granted turns (see the constants
    block — a fixed backstop made large turn budgets unreachable). ``None`` /
    non-positive falls back to the default turn budget (the schema enforces
    ``ge=1`` for real calls, but the routing edge must stay robust to a
    malformed args dict).
    """
    if not isinstance(budget, int) or budget < MIN_SUBAGENT_TURN_BUDGET:
        turns = DEFAULT_SUBAGENT_TURN_BUDGET
    else:
        turns = min(budget, MAX_SUBAGENT_TURN_BUDGET)
    return SubagentCeiling(
        max_turns=turns, max_tokens=turns * SUBAGENT_TOKEN_CEILING_PER_TURN
    )


async def _dispatch_subagent_unreachable(*args, **kwargs):  # pragma: no cover
    """Defensive stub — the routing edge intercepts the call before the ToolNode.

    If this ever runs, the post-agent routing edge failed to split a
    ``dispatch_subagent`` call out of the ToolNode path — a graph-wiring bug.
    """
    raise RuntimeError(
        "dispatch_subagent must be routed via Send to the subagent node, not "
        "executed in the ToolNode — this stub is unreachable by design. "
        "A post-agent routing edge in executor/graph.py intercepts the call."
    )


def build_dispatch_subagent_tool() -> StructuredTool:
    """Build the LLM-facing ``dispatch_subagent`` tool (schema only).

    No runtime context is closure-bound into the tool itself: the model handle,
    identifiers, event-emit callable, depth, and checkpointer all live on the
    routing edge / subagent node in :mod:`executor.graph`, which is what
    actually performs the delegation. The tool only needs to expose the schema
    so the model can emit the call.
    """
    return StructuredTool.from_function(
        coroutine=_dispatch_subagent_unreachable,
        name=DISPATCH_SUBAGENT_TOOL_NAME,
        description=DISPATCH_SUBAGENT_DESCRIPTION,
        args_schema=DispatchSubagentArguments,
    )
