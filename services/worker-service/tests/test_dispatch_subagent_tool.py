"""Unit tests for the ``dispatch_subagent`` delegation tool (Task S4).

S4 is the **Topology-1 driver** over S3's shared ``run_subagent`` helper. These
tests assert the load-bearing surface S4 adds:

* allowlist-gated registration of the LLM-facing schema in ``_get_tools``;
* the **post-agent routing edge** that intercepts a ``dispatch_subagent`` tool
  call and ``Send``s it to the shared subagent node — NOT the ToolNode — with
  ``budget → ceiling`` arg pass-through and ``depth`` sourced from graph state
  (never the LLM args, so it cannot be escalated);
* **mixed-turn splitting** — a dispatch call alongside a normal tool call routes
  the dispatch via ``Send`` and the normal call to the ToolNode, so every
  ``tool_call_id`` is answered exactly once before the next agent-node call;
* the subagent node threading a **``ToolMessage`` keyed to the original
  ``tool_call_id``** — success → summary, failure marker → descriptive message
  (never a raised graph error) — while the sub-agent's internal messages stay on
  S3's separate channel (isolation: the parent ``messages`` never sees them);
* ``MAX_TOOLS_PER_AGENT`` is respected (no special exemption).

These do not re-test S3's ceiling/timeout/depth ENFORCEMENT — that is
``test_subagent_fanout.py``. S4 tests assert routing-edge delegation +
``tool_call_id``-keyed result injection + mixed-turn answering + gating.

Worktree-concurrency-safe: binds no TCP ports, spawns no server subprocess.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.types import Send
from pydantic import BaseModel, Field

from core.config import WorkerConfig
from executor.graph import GraphExecutor
from executor.memory_graph import MemoryDecision
from executor.schema_converter import MAX_TOOLS_PER_AGENT
from executor.subagents import SubagentCeiling, SubagentResult
from tools.subagent_tools import (
    DEFAULT_SUBAGENT_TURN_BUDGET,
    DISPATCH_SUBAGENT_TOOL_NAME,
    MAX_SUBAGENT_TURN_BUDGET,
    budget_to_ceiling,
    build_dispatch_subagent_tool,
)

# ``asyncio_mode = "auto"`` (pyproject.toml) runs async tests without a marker.


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_executor() -> GraphExecutor:
    return GraphExecutor(WorkerConfig(worker_id="test-worker", tenant_id="default"), MagicMock())


async def _build(allowed_tools: list[str], *, checkpointer=None, custom_tools=None):
    """Build the main workflow with a patched LLM (no network)."""
    executor = _make_executor()
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock()
    with patch("executor.providers.create_llm", AsyncMock(return_value=llm)):
        workflow = await executor._build_graph(
            {"model": "claude-haiku-4-5", "allowed_tools": allowed_tools},
            cancel_event=asyncio.Event(),
            task_id="t",
            tenant_id="default",
            agent_id="a",
            memory_decision=MemoryDecision(stack_enabled=False, auto_write=False),
            task_input="x",
            checkpointer=checkpointer,
            custom_tools=custom_tools,
        )
    return workflow


def _route_fn(workflow):
    """Return a callable evaluating the agent node's conditional edge."""
    branch = next(iter((workflow.branches.get("agent") or {}).values()))
    return lambda state: branch.path.invoke(state)


def _ai_with_calls(calls: list[dict]) -> AIMessage:
    msg = AIMessage(content="")
    msg.tool_calls = calls
    return msg


def _dispatch_call(call_id: str, *, prompt="investigate", tools=None, budget=5) -> dict:
    return {
        "id": call_id,
        "name": DISPATCH_SUBAGENT_TOOL_NAME,
        "args": {"prompt": prompt, "tools": tools or [], "budget": budget},
    }


def _normal_tool() -> StructuredTool:
    class _NoArgs(BaseModel):
        text: str = Field(default="x")

    async def _echo(text: str = "x") -> str:
        return f"echoed:{text}"

    return StructuredTool.from_function(
        coroutine=_echo, name="echo", description="echo", args_schema=_NoArgs
    )


# --------------------------------------------------------------------------- #
# 1. Allowlist-gated registration
# --------------------------------------------------------------------------- #
class TestRegistrationGate:
    def _tool_names(self, allowed_tools: list[str]) -> list[str]:
        executor = object.__new__(GraphExecutor)
        tools = executor._get_tools(
            allowed_tools,
            cancel_event=asyncio.Event(),
            task_id="t",
            tenant_id="default",
            agent_id="a",
        )
        return [t.name for t in tools]

    def test_registered_when_allowlisted(self) -> None:
        names = self._tool_names([DISPATCH_SUBAGENT_TOOL_NAME])
        assert names.count(DISPATCH_SUBAGENT_TOOL_NAME) == 1

    def test_absent_when_not_allowlisted(self) -> None:
        names = self._tool_names(["web_search"])
        assert DISPATCH_SUBAGENT_TOOL_NAME not in names

    def test_absent_with_empty_allowlist(self) -> None:
        names = self._tool_names([])
        assert DISPATCH_SUBAGENT_TOOL_NAME not in names

    def test_schema_excludes_depth(self) -> None:
        tool = build_dispatch_subagent_tool()
        fields = set(tool.args_schema.model_fields.keys())
        assert fields == {"prompt", "tools", "budget"}
        assert "depth" not in fields


# --------------------------------------------------------------------------- #
# 2. budget → ceiling mapping
# --------------------------------------------------------------------------- #
class TestBudgetToCeiling:
    def test_budget_becomes_turn_ceiling(self) -> None:
        c = budget_to_ceiling(5)
        assert isinstance(c, SubagentCeiling)
        assert c.max_turns == 5

    def test_budget_clamped_to_max(self) -> None:
        assert budget_to_ceiling(10_000).max_turns == MAX_SUBAGENT_TURN_BUDGET

    def test_none_or_invalid_falls_back_to_default(self) -> None:
        assert budget_to_ceiling(None).max_turns == DEFAULT_SUBAGENT_TURN_BUDGET
        assert budget_to_ceiling(0).max_turns == DEFAULT_SUBAGENT_TURN_BUDGET
        assert budget_to_ceiling("nope").max_turns == DEFAULT_SUBAGENT_TURN_BUDGET  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 3. Routing edge — Send to the subagent node, NOT the ToolNode
# --------------------------------------------------------------------------- #
class TestRoutingEdge:
    async def test_subagent_node_registered_when_enabled(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME])
        assert "subagent" in wf.nodes
        # The subagent node returns to the agent node so the loop continues.
        assert "agent" in wf.nodes

    async def test_no_subagent_node_when_disabled(self) -> None:
        wf = await _build(["web_search"])
        assert "subagent" not in wf.nodes

    async def test_dispatch_call_routes_via_send_not_toolnode(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME])
        route = _route_fn(wf)
        state = {"messages": [_ai_with_calls([_dispatch_call("d1")])]}
        result = route(state)
        assert isinstance(result, list)
        sends = [s for s in result if isinstance(s, Send)]
        assert len(sends) == 1
        assert sends[0].node == "subagent"
        # The normal ToolNode target is absent — this turn has no normal calls.
        assert "tools" not in result

    async def test_send_payload_passes_budget_prompt_tools(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME])
        route = _route_fn(wf)
        call = _dispatch_call("d1", prompt="find flaky test", tools=["echo"], budget=7)
        result = route({"messages": [_ai_with_calls([call])]})
        send = next(s for s in result if isinstance(s, Send))
        arg = send.arg
        assert arg["tool_call_id"] == "d1"
        assert arg["prompt"] == "find flaky test"
        assert arg["tools"] == ["echo"]
        # budget passed through verbatim; ceiling translation happens in-node.
        assert arg["budget"] == 7

    async def test_depth_sourced_from_state_and_incremented(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME])
        route = _route_fn(wf)
        call = _dispatch_call("d1")
        # Default (no depth in state) → parent depth 0 → sub-agent depth 1.
        r0 = route({"messages": [_ai_with_calls([call])]})
        assert next(s for s in r0 if isinstance(s, Send)).arg["depth"] == 1
        # Parent already at depth 1 → sub-agent depth 2.
        r1 = route({"messages": [_ai_with_calls([call])], "depth": 1})
        assert next(s for s in r1 if isinstance(s, Send)).arg["depth"] == 2

    async def test_depth_cannot_be_escalated_via_llm_args(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME])
        route = _route_fn(wf)
        # A crafted call injecting a huge depth in args must be ignored — depth
        # comes from state only.
        call = _dispatch_call("d1")
        call["args"]["depth"] = 99
        send = next(
            s for s in route({"messages": [_ai_with_calls([call])]}) if isinstance(s, Send)
        )
        assert send.arg["depth"] == 1  # state(0) + 1, NOT 99

    async def test_non_dispatch_turn_routes_to_tools_string(self) -> None:
        # With dispatch enabled but the turn emitting only a normal tool call,
        # the route returns the plain "tools" string (no Send).
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME], custom_tools=[_normal_tool()])
        route = _route_fn(wf)
        normal = {"id": "n1", "name": "echo", "args": {"text": "hi"}}
        result = route({"messages": [_ai_with_calls([normal])]})
        assert result == "tools"

    async def test_no_tool_calls_routes_to_end(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME])
        route = _route_fn(wf)
        assert route({"messages": [AIMessage(content="all done")]}) == END


# --------------------------------------------------------------------------- #
# 4. Mixed turn — dispatch via Send AND normal tool via ToolNode
# --------------------------------------------------------------------------- #
class TestMixedTurn:
    async def test_dispatch_and_normal_both_routed(self) -> None:
        wf = await _build([DISPATCH_SUBAGENT_TOOL_NAME], custom_tools=[_normal_tool()])
        route = _route_fn(wf)
        disp = _dispatch_call("d1")
        normal = {"id": "n1", "name": "echo", "args": {"text": "hi"}}
        result = route({"messages": [_ai_with_calls([disp, normal])]})
        assert isinstance(result, list)
        sends = [s for s in result if isinstance(s, Send)]
        # Exactly one Send to the subagent node for the dispatch call.
        assert len(sends) == 1 and sends[0].node == "subagent"
        assert sends[0].arg["tool_call_id"] == "d1"
        # The "tools" node string is present for the normal call.
        assert "tools" in result

    async def test_every_tool_call_id_answered_exactly_once_end_to_end(self) -> None:
        """Compile + run: every emitted tool_call_id gets exactly one ToolMessage
        before the next agent-node call (an unanswered call → provider error)."""
        saver = MemorySaver()

        # Scripted model: turn 1 emits dispatch + echo; turn 2 finishes.
        scripted = [
            _ai_with_calls(
                [
                    _dispatch_call("disp1", prompt="investigate"),
                    {"id": "echo1", "name": "echo", "args": {"text": "hi"}},
                ]
            ),
            AIMessage(content="all done"),
        ]
        state = {"i": 0}

        async def fake_ainvoke(messages, *a, **k):
            i = state["i"]
            state["i"] += 1
            return scripted[min(i, len(scripted) - 1)]

        executor = _make_executor()
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        llm.ainvoke = AsyncMock(side_effect=fake_ainvoke)

        async def fake_run_subagent(prompt, tools, **kw):
            return SubagentResult.success("sub summary")

        with patch("executor.providers.create_llm", AsyncMock(return_value=llm)), patch(
            "executor.graph.run_subagent", AsyncMock(side_effect=fake_run_subagent)
        ):
            workflow = await executor._build_graph(
                {"model": "claude-haiku-4-5", "allowed_tools": [DISPATCH_SUBAGENT_TOOL_NAME]},
                cancel_event=asyncio.Event(),
                task_id="t",
                tenant_id="default",
                agent_id="a",
                memory_decision=MemoryDecision(stack_enabled=False, auto_write=False),
                task_input="x",
                checkpointer=saver,
                custom_tools=[_normal_tool()],
            )
            app = workflow.compile(checkpointer=saver)
            final = await app.ainvoke(
                {"messages": [AIMessage(content="seed")]},
                config={"configurable": {"thread_id": "mixed"}},
                durability="sync",
            )

        emitted = {
            tc["id"]
            for m in final["messages"]
            if isinstance(m, AIMessage)
            for tc in (m.tool_calls or [])
        }
        answered = [
            m.tool_call_id for m in final["messages"] if isinstance(m, ToolMessage)
        ]
        assert emitted == {"disp1", "echo1"}
        # Each id answered exactly once.
        assert sorted(answered) == sorted(emitted)
        assert len(answered) == len(set(answered))


# --------------------------------------------------------------------------- #
# 5. Subagent node — result threading, isolation, failure markers
# --------------------------------------------------------------------------- #
class TestSubagentNodeThreading:
    async def _node(self, allowed_tools=None, custom_tools=None):
        wf = await _build(
            allowed_tools or [DISPATCH_SUBAGENT_TOOL_NAME],
            checkpointer=MemorySaver(),
            custom_tools=custom_tools,
        )
        return wf.nodes["subagent"].runnable

    async def test_success_threads_summary_keyed_to_tool_call_id(self) -> None:
        node = await self._node()
        with patch(
            "executor.graph.run_subagent",
            AsyncMock(return_value=SubagentResult.success("the answer is 42")),
        ):
            out = await node.ainvoke(
                {"tool_call_id": "d1", "prompt": "q", "tools": [], "budget": 5, "depth": 1},
                {"configurable": {"thread_id": "t"}},
            )
        msgs = out["messages"]
        assert len(msgs) == 1
        assert isinstance(msgs[0], ToolMessage)
        assert msgs[0].tool_call_id == "d1"
        assert "42" in msgs[0].content

    async def test_internal_messages_not_on_parent_channel(self) -> None:
        """Isolation: the node returns ONLY the summary ToolMessage — none of
        the sub-agent's internal working messages reach the parent channel."""
        node = await self._node()
        with patch(
            "executor.graph.run_subagent",
            AsyncMock(return_value=SubagentResult.success("summary only")),
        ):
            out = await node.ainvoke(
                {"tool_call_id": "d1", "prompt": "q", "tools": [], "budget": 5, "depth": 1},
                {"configurable": {"thread_id": "t"}},
            )
        # Exactly one message, the summary ToolMessage — no AIMessages, no
        # intermediate ToolMessages from the sub-agent's inner turns.
        assert [type(m).__name__ for m in out["messages"]] == ["ToolMessage"]

    @pytest.mark.parametrize("reason", ["ceiling", "timeout", "error", "depth"])
    async def test_failure_marker_threads_toolmessage_not_raise(self, reason) -> None:
        node = await self._node()
        with patch(
            "executor.graph.run_subagent",
            AsyncMock(return_value=SubagentResult.failure(reason, detail="boom")),
        ):
            out = await node.ainvoke(
                {"tool_call_id": "d1", "prompt": "q", "tools": [], "budget": 5, "depth": 1},
                {"configurable": {"thread_id": "t"}},
            )
        msg = out["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "d1"
        # The reason is surfaced so the parent LLM can react; no exception.
        assert reason in msg.content

    async def test_node_passes_budget_as_ceiling_and_depth_from_payload(self) -> None:
        """The node translates budget→SubagentCeiling and forwards the payload
        depth into run_subagent (which enforces MAX_SUBAGENT_DEPTH)."""
        node = await self._node()
        spy = AsyncMock(return_value=SubagentResult.success("ok"))
        with patch("executor.graph.run_subagent", spy):
            await node.ainvoke(
                {"tool_call_id": "d1", "prompt": "p", "tools": [], "budget": 9, "depth": 2},
                {"configurable": {"thread_id": "parent-thread"}},
            )
        _, kwargs = spy.call_args
        assert isinstance(kwargs["ceiling"], SubagentCeiling)
        assert kwargs["ceiling"].max_turns == 9
        assert kwargs["depth"] == 2
        # Sub-thread derived off the parent thread for the namespaced checkpoint.
        assert "parent-thread" in kwargs["thread_id"]
        assert "d1" in kwargs["thread_id"]

    async def test_node_delegates_only_parent_tools_and_drops_dispatch(self) -> None:
        """A sub-agent can only use tools the parent has; dispatch_subagent is
        never delegated (and tools the parent lacks are ignored)."""
        node = await self._node(
            allowed_tools=[DISPATCH_SUBAGENT_TOOL_NAME], custom_tools=[_normal_tool()]
        )
        spy = AsyncMock(return_value=SubagentResult.success("ok"))
        with patch("executor.graph.run_subagent", spy):
            await node.ainvoke(
                {
                    "tool_call_id": "d1",
                    "prompt": "p",
                    # Request echo (parent has it), dispatch_subagent (must drop),
                    # and a tool the parent lacks (must drop).
                    "tools": ["echo", DISPATCH_SUBAGENT_TOOL_NAME, "not_a_real_tool"],
                    "budget": 5,
                    "depth": 1,
                },
                {"configurable": {"thread_id": "t"}},
            )
        args, _ = spy.call_args
        delegated = args[1]  # positional: (prompt, tools)
        names = {getattr(t, "name", None) for t in delegated}
        assert names == {"echo"}


# --------------------------------------------------------------------------- #
# 6. MAX_TOOLS_PER_AGENT respected
# --------------------------------------------------------------------------- #
class TestToolCap:
    async def test_dispatch_counts_against_cap(self) -> None:
        """Registering dispatch_subagent is subject to MAX_TOOLS_PER_AGENT —
        an agent already at the cap raises rather than silently exceeding it."""
        # Fill custom_tools to exactly the cap, then add dispatch_subagent via
        # the allowlist → total exceeds cap → existing ValueError.
        class _NoArgs(BaseModel):
            x: str = Field(default="x")

        def _mk(i):
            def _f(x: str = "x") -> str:
                return "ok"

            return StructuredTool.from_function(
                func=_f, name=f"tool_{i}", description="t", args_schema=_NoArgs
            )

        custom = [_mk(i) for i in range(MAX_TOOLS_PER_AGENT)]
        with pytest.raises(ValueError, match="max"):
            await _build([DISPATCH_SUBAGENT_TOOL_NAME], custom_tools=custom)


# --------------------------------------------------------------------------- #
# 7. Pattern-B symbol hygiene (grep-asserted on the new module)
# --------------------------------------------------------------------------- #
def test_no_pattern_b_symbols_in_subagent_tools_module() -> None:
    """Pattern A only — none of Pattern B's cross-task spawn-and-await
    identifiers may appear in the new tool module."""
    src = Path(__file__).parent.parent / "tools" / "subagent_tools.py"
    text = src.read_text()
    for forbidden in ("sub_agent_id", "parent_task_id", "waiting_for_subagent"):
        assert forbidden not in text, f"Pattern-B symbol {forbidden!r} leaked into subagent_tools.py"
