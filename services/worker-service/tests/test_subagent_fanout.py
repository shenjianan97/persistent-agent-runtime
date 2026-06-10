"""Unit tests for the shared in-process fan-out helper ``run_subagent`` (Task S3).

All tests here are **fake-model, no-network, no-DB** — they run the sub-agent
ReAct subgraph on an in-process ``MemorySaver``. The single Postgres-backed
durability confirmation lives in
``tests/backend-integration/test_subagent_fanout_durability.py`` (run via the
isolated ``make e2e-test`` harness), per the S3 spec.

Worktree-concurrency-safe: binds no TCP ports, spawns no server subprocess.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver

from executor.subagents import (
    MAX_SUBAGENT_DEPTH,
    SUBAGENT_HEARTBEAT_EVENT,
    SubagentCeiling,
    SubagentResult,
    filter_headless_tools,
    run_subagent,
)

# ``asyncio_mode = "auto"`` (pyproject.toml) runs async tests without an
# explicit marker — and avoids marking the sync grep-as-test functions.


# --------------------------------------------------------------------------- #
# Fakes — a tool-calling chat model driven by a scripted list of AIMessages.
# --------------------------------------------------------------------------- #
class FakeModel:
    """Minimal injectable chat model.

    ``ainvoke`` returns the next scripted ``AIMessage`` (cycling on the final
    one so a runaway loop keeps producing tool calls). ``bind_tools`` records
    what it was bound with and returns ``self`` so the loop can keep calling.
    """

    def __init__(self, scripted: list[AIMessage]):
        self._scripted = scripted
        self._i = 0
        self.bound_tools: list[Any] | None = None
        self.invocations = 0

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        self.invocations += 1
        template = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        # Return a FRESH message each call with unique ids — a real provider
        # never returns the same message instance twice, and ``add_messages``
        # dedupes by id (reusing one instance would collapse a loop).
        tool_calls = [
            {**tc, "id": f"{tc['id']}-{self.invocations}"}
            for tc in (template.tool_calls or [])
        ]
        return AIMessage(
            id=f"ai-{self.invocations}",
            content=template.content,
            tool_calls=tool_calls,
            usage_metadata=template.usage_metadata,
        )


def _ai_final(text: str, *, tokens: int = 5) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": tokens, "output_tokens": tokens,
                        "total_tokens": tokens * 2},
    )


def _ai_tool_call(call_id: str, name: str, args: dict, *, tokens: int = 5) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata={"input_tokens": tokens, "output_tokens": tokens,
                        "total_tokens": tokens * 2},
    )


def _echo_tool() -> StructuredTool:
    async def echo(text: str) -> str:
        return f"echoed:{text}"

    return StructuredTool.from_function(coroutine=echo, name="echo",
                                        description="echo the text back")


def _human_input_tool() -> StructuredTool:
    """A stand-in for the interrupt()-bearing ``request_human_input`` tool.

    Named identically to the real worker tool so the headless filter targets
    it by name.
    """
    async def request_human_input(prompt: str) -> str:
        return "should never run in a sub-agent"

    return StructuredTool.from_function(
        coroutine=request_human_input,
        name="request_human_input",
        description="pause for a human (interrupt-bearing)",
    )


class SpyEmit:
    """Records (event_name, payload) tuples for heartbeat assertions."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_name: str, payload: dict | None = None):
        self.events.append((event_name, payload or {}))


def _saver() -> MemorySaver:
    return MemorySaver()


def _ceiling(max_turns: int = 8, max_tokens: int = 100_000) -> SubagentCeiling:
    return SubagentCeiling(max_turns=max_turns, max_tokens=max_tokens)


# --------------------------------------------------------------------------- #
# 1. Success path
# --------------------------------------------------------------------------- #
async def test_success_returns_structured_summary():
    model = FakeModel([_ai_final("the answer is 42")])
    result = await run_subagent(
        prompt="what is the answer",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-success",
        emit=SpyEmit(),
    )
    assert isinstance(result, SubagentResult)
    assert result.ok is True
    assert result.reason is None
    assert "42" in result.summary


async def test_success_carries_accumulated_usage():
    # S8 (§A11-E1): run_subagent must surface the sub-agent's accumulated
    # usage so the fan-out node can bill it to the parent super-step. Two LLM
    # turns (tool call + final), each 10 in / 7 out → 20 in / 14 out total.
    model = FakeModel([
        _ai_tool_call("c1", "echo", {"text": "hi"}, tokens=0),
        _ai_final("done", tokens=0),
    ])
    # Override usage_metadata explicitly to known asymmetric values.
    model._scripted = [
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "x"}, "id": "c1"}],
                  usage_metadata={"input_tokens": 10, "output_tokens": 7, "total_tokens": 17}),
        AIMessage(content="done",
                  usage_metadata={"input_tokens": 10, "output_tokens": 7, "total_tokens": 17}),
    ]
    result = await run_subagent(
        prompt="use the tool then answer",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-usage",
        emit=SpyEmit(),
    )
    assert result.ok is True
    assert result.usage == {"input_tokens": 20, "output_tokens": 14}


async def test_failure_marker_has_empty_usage():
    # A depth-rejected sub-agent never invokes a model → empty usage.
    result = await run_subagent(
        prompt="x",
        tools=[],
        ceiling=_ceiling(),
        depth=MAX_SUBAGENT_DEPTH + 1,
        model=FakeModel([_ai_final("never")]),
        checkpointer=_saver(),
        thread_id="t-depth",
        emit=SpyEmit(),
    )
    assert result.ok is False
    assert result.usage == {}


async def test_success_runs_a_tool_then_answers():
    model = FakeModel([
        _ai_tool_call("c1", "echo", {"text": "hi"}),
        _ai_final("done after tool"),
    ])
    result = await run_subagent(
        prompt="use the tool then answer",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-tool",
        emit=SpyEmit(),
    )
    assert result.ok is True
    assert "done after tool" in result.summary


# --------------------------------------------------------------------------- #
# 2. Ceiling by turns
# --------------------------------------------------------------------------- #
async def test_ceiling_by_turns_returns_failure_marker():
    # The model never stops calling the tool -> turn cap must trip.
    model = FakeModel([_ai_tool_call("c1", "echo", {"text": "loop"})])
    result = await run_subagent(
        prompt="loop forever",
        tools=[_echo_tool()],
        ceiling=_ceiling(max_turns=3, max_tokens=10_000_000),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-turns",
        emit=SpyEmit(),
    )
    assert result.ok is False
    assert result.reason == "ceiling"


async def test_high_turn_ceiling_is_binding_not_recursion_limit():
    # A turn cap above LangGraph's default recursion_limit (25) must still trip
    # the TURN ceiling, not a GraphRecursionError surfacing as an ``error``.
    model = FakeModel([_ai_tool_call("c1", "echo", {"text": "loop"})])
    result = await run_subagent(
        prompt="loop a lot",
        tools=[_echo_tool()],
        ceiling=_ceiling(max_turns=40, max_tokens=10_000_000),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-high-turns",
        emit=SpyEmit(),
    )
    assert result.ok is False
    assert result.reason == "ceiling"
    assert model.invocations == 40


# --------------------------------------------------------------------------- #
# 3. Ceiling by tokens
# --------------------------------------------------------------------------- #
async def test_ceiling_by_tokens_returns_failure_marker():
    # Each turn burns 200 total tokens; a 300-token cap trips on turn 2.
    model = FakeModel([_ai_tool_call("c1", "echo", {"text": "loop"}, tokens=100)])
    result = await run_subagent(
        prompt="burn tokens",
        tools=[_echo_tool()],
        ceiling=_ceiling(max_turns=10_000, max_tokens=300),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-tokens",
        emit=SpyEmit(),
    )
    assert result.ok is False
    assert result.reason == "ceiling"


# --------------------------------------------------------------------------- #
# 4. Timeout
# --------------------------------------------------------------------------- #
async def test_timeout_returns_failure_marker():
    slow_started = asyncio.Event()

    async def slow(text: str) -> str:
        slow_started.set()
        await asyncio.sleep(30)  # far longer than the injected timeout
        return "never"

    slow_tool = StructuredTool.from_function(coroutine=slow, name="echo",
                                             description="slow tool")
    model = FakeModel([
        _ai_tool_call("c1", "echo", {"text": "hi"}),
        _ai_final("unreached"),
    ])
    result = await run_subagent(
        prompt="hang",
        tools=[slow_tool],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-timeout",
        emit=SpyEmit(),
        timeout_seconds=0.2,
    )
    assert result.ok is False
    assert result.reason == "timeout"


# --------------------------------------------------------------------------- #
# 5. Depth rejection (no spawn)
# --------------------------------------------------------------------------- #
async def test_depth_over_cap_rejected_without_spawn():
    model = FakeModel([_ai_final("should not run")])
    result = await run_subagent(
        prompt="too deep",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=MAX_SUBAGENT_DEPTH + 1,
        model=model,
        checkpointer=_saver(),
        thread_id="t-depth",
        emit=SpyEmit(),
    )
    assert result.ok is False
    assert result.reason == "depth"
    # No spawn: the model was never bound or invoked.
    assert model.bound_tools is None
    assert model.invocations == 0


async def test_depth_at_cap_is_allowed():
    model = FakeModel([_ai_final("ok at the cap")])
    result = await run_subagent(
        prompt="at the cap",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=MAX_SUBAGENT_DEPTH,
        model=model,
        checkpointer=_saver(),
        thread_id="t-depth-ok",
        emit=SpyEmit(),
    )
    assert result.ok is True


# --------------------------------------------------------------------------- #
# 6. Headless filter — interrupt-bearing tools are not bound
# --------------------------------------------------------------------------- #
def test_filter_headless_drops_request_human_input():
    tools = [_echo_tool(), _human_input_tool()]
    filtered = filter_headless_tools(tools)
    names = {t.name for t in filtered}
    assert "request_human_input" not in names
    assert "echo" in names


async def test_subagent_never_binds_interrupt_tool():
    model = FakeModel([_ai_final("answer")])
    await run_subagent(
        prompt="answer me",
        tools=[_echo_tool(), _human_input_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-headless",
        emit=SpyEmit(),
    )
    assert model.bound_tools is not None
    bound_names = {t.name for t in model.bound_tools}
    assert "request_human_input" not in bound_names
    assert "echo" in bound_names


# --------------------------------------------------------------------------- #
# 6b. Headless filter — parent-state-channel tools are not bound
#
# ``plan_write`` / ``note_finding`` / ``remember_this_run`` return a LangGraph
# ``Command`` targeting PARENT channels (``plan`` / ``messages`` /
# ``observations``) that do not exist in the isolated ``_SubagentState``, and
# declare ``InjectedToolCallId`` (which the slim sub-agent tool loop does not
# inject). Regression for task d78a6d74: all 3 fanned-out sub-agents called
# ``plan_write`` in round 1, each raised
# ``ValueError: When tool includes an InjectedToolCallId argument ...``, and
# the supervisor dead-lettered the task.
# --------------------------------------------------------------------------- #
def _real_plan_write_tool() -> StructuredTool:
    from tools.plan_tools import build_plan_write_tool

    return build_plan_write_tool()


def _named_stub_tool(name: str) -> StructuredTool:
    async def stub(text: str) -> str:
        return "should never be bound in a sub-agent"

    return StructuredTool.from_function(
        coroutine=stub, name=name, description=f"stand-in for {name}"
    )


def test_filter_headless_drops_parent_state_tools():
    tools = [
        _echo_tool(),
        _real_plan_write_tool(),
        _named_stub_tool("note_finding"),
        _named_stub_tool("remember_this_run"),
    ]
    filtered = filter_headless_tools(tools)
    names = {t.name for t in filtered}
    assert "plan_write" not in names
    assert "note_finding" not in names
    assert "remember_this_run" not in names
    assert "echo" in names


async def test_subagent_survives_scripted_plan_write_call():
    # The model calls plan_write (as every research sub-agent did in the
    # dead-lettered run); the tool must not be bound, the loop must answer the
    # call with an in-band error ToolMessage, and the run must still succeed.
    model = FakeModel([
        _ai_tool_call(
            "c1", "plan_write",
            {"items": [{"id": "1", "title": "step", "status": "pending"}]},
        ),
        _ai_final("research summary"),
    ])
    result = await run_subagent(
        prompt="research something",
        tools=[_echo_tool(), _real_plan_write_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-plan-write-regression",
        emit=SpyEmit(),
    )
    assert model.bound_tools is not None
    assert "plan_write" not in {t.name for t in model.bound_tools}
    assert result.ok is True, f"reason={result.reason} summary={result.summary}"
    assert "research summary" in result.summary


# --------------------------------------------------------------------------- #
# 6b'. Subtask identity — the caller's stable id is persisted in the
# sub-checkpoint state so read-side projections (Console /activity sub-agent
# tree) can correlate the opaque ``subagent:<uuid>`` namespace back to the
# Supervisor's deterministic "<iteration>.<index>" subtask id.
# --------------------------------------------------------------------------- #
async def test_subtask_id_is_persisted_in_sub_checkpoint():
    saver = _saver()
    model = FakeModel([_ai_final("done")])
    result = await run_subagent(
        prompt="focused subtask",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=saver,
        thread_id="t-subtask-id",
        emit=SpyEmit(),
        subtask="1.2",
    )
    assert result.ok is True
    # Namespace keys come from internal storage; content via the public list()
    # API so serialization stays the saver's concern.
    namespaces = list(saver.storage.get("t-subtask-id", {}))
    sub_ns = [ns for ns in namespaces if ns.startswith("subagent:")]
    assert sub_ns, f"no sub-agent namespace checkpointed; got {namespaces}"
    found = False
    for ns in sub_ns:
        cfg = {"configurable": {"thread_id": "t-subtask-id", "checkpoint_ns": ns}}
        for ckpt_tuple in saver.list(cfg):
            values = (ckpt_tuple.checkpoint or {}).get("channel_values", {})
            if values.get("subtask") == "1.2":
                found = True
    assert found, "subtask id missing from sub-checkpoint channel_values"


async def test_subtask_omitted_leaves_channel_absent():
    saver = _saver()
    model = FakeModel([_ai_final("done")])
    await run_subagent(
        prompt="no id",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=saver,
        thread_id="t-no-subtask",
        emit=SpyEmit(),
    )
    namespaces = list(saver.storage.get("t-no-subtask", {}))
    for ns in namespaces:
        if not ns.startswith("subagent:"):
            continue
        cfg = {"configurable": {"thread_id": "t-no-subtask", "checkpoint_ns": ns}}
        for ckpt_tuple in saver.list(cfg):
            values = (ckpt_tuple.checkpoint or {}).get("channel_values", {})
            assert "subtask" not in values


# --------------------------------------------------------------------------- #
# 6b''. Transient LLM-error retry — one provider timeout must not forfeit the
# sub-agent. Regression for task 0729e3a3 sub-agent 1.3: a single Bedrock
# ReadTimeoutError on its FINAL model call (after 17 turns of completed
# research) became a permanent failure because ``run_subagent`` swallows
# exceptions into a failure marker, cutting the branch off from the task-level
# retry+resume protection the main agent enjoys. Note: LangGraph's DEFAULT
# RetryPolicy predicate refuses botocore's ReadTimeoutError (OSError is in its
# MRO), so the policy must use the custom transient predicate.
# --------------------------------------------------------------------------- #
class _FlakyOnceModel(FakeModel):
    """Raises the given exception on the FIRST ainvoke, then behaves normally."""

    def __init__(self, scripted, exc: Exception):
        super().__init__(scripted)
        self._exc: Exception | None = exc

    async def ainvoke(self, messages, *args, **kwargs):
        if self._exc is not None:
            exc, self._exc = self._exc, None
            self.invocations += 1
            raise exc
        return await super().ainvoke(messages, *args, **kwargs)


async def test_transient_llm_error_is_retried():
    from botocore.exceptions import ReadTimeoutError

    model = _FlakyOnceModel(
        [_ai_final("recovered findings")],
        ReadTimeoutError(endpoint_url="https://bedrock-runtime/converse"),
    )
    result = await run_subagent(
        prompt="research",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-transient-retry",
        emit=SpyEmit(),
    )
    assert result.ok is True, f"reason={result.reason} summary={result.summary}"
    assert "recovered findings" in result.summary
    assert model.invocations == 2  # first call raised, retry succeeded


async def test_non_transient_llm_error_is_not_retried():
    model = _FlakyOnceModel(
        [_ai_final("never reached")],
        ValueError("malformed tool schema — retrying cannot help"),
    )
    result = await run_subagent(
        prompt="research",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-non-transient",
        emit=SpyEmit(),
    )
    assert result.ok is False
    assert result.reason == "error"
    assert model.invocations == 1  # no retry on logic errors
    # The typed cause is captured as the failure detail so the Console marker
    # can show WHY (not just "error").
    assert "ValueError" in result.summary
    assert "malformed tool schema" in result.summary


def test_transient_predicate_covers_observed_and_common_shapes():
    from botocore.exceptions import ReadTimeoutError

    from executor.retry_classification import is_retryable_error
    from executor.subagents.fanout import _SUBAGENT_LLM_RETRY

    # The per-turn RetryPolicy must gate on the SHARED classifier — the
    # whole point of the extraction is that fanout and the task-level
    # dead-letter decision can never drift apart again.
    assert _SUBAGENT_LLM_RETRY.retry_on is is_retryable_error

    # The exact exception that killed sub-agent 1.3 live.
    assert is_retryable_error(ReadTimeoutError(endpoint_url="x")) is True
    # Builtin transport errors.
    assert is_retryable_error(ConnectionError("reset")) is True
    assert is_retryable_error(TimeoutError("slow")) is True
    # Logic errors must not retry.
    assert is_retryable_error(ValueError("bad args")) is False
    assert is_retryable_error(KeyError("missing")) is False


# --------------------------------------------------------------------------- #
# 6c. Tool-error containment — a raising tool yields an error ToolMessage,
# it must NOT kill the whole sub-agent (ToolNode parity: the parent graph's
# prebuilt ToolNode surfaces tool errors in-band for the LLM to route around).
# --------------------------------------------------------------------------- #
async def test_tool_exception_becomes_error_toolmessage():
    def _raising_tool() -> StructuredTool:
        async def boom(text: str) -> str:
            raise RuntimeError("transient provider failure")

        return StructuredTool.from_function(
            coroutine=boom, name="boom", description="always raises"
        )

    model = FakeModel([
        _ai_tool_call("c1", "boom", {"text": "x"}),
        _ai_final("recovered after tool error"),
    ])
    result = await run_subagent(
        prompt="try the flaky tool",
        tools=[_echo_tool(), _raising_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-tool-error-contained",
        emit=SpyEmit(),
    )
    assert result.ok is True, f"reason={result.reason} summary={result.summary}"
    assert "recovered" in result.summary


# --------------------------------------------------------------------------- #
# 7. Heartbeat — span event only, no lease/heartbeat/task_events touch
# --------------------------------------------------------------------------- #
async def test_heartbeat_emitted_as_span_event(monkeypatch):
    # Spy on core.heartbeat to prove the helper never calls into it.
    import core.heartbeat as hb_mod

    called = {"build_query": 0}
    orig = hb_mod.build_heartbeat_query

    def _tripwire(*a, **k):
        called["build_query"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(hb_mod, "build_heartbeat_query", _tripwire)

    spy = SpyEmit()
    # Two tool turns then an answer -> at least one heartbeat between turns.
    model = FakeModel([
        _ai_tool_call("c1", "echo", {"text": "a"}),
        _ai_tool_call("c2", "echo", {"text": "b"}),
        _ai_final("done"),
    ])
    result = await run_subagent(
        prompt="long running",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-heartbeat",
        emit=spy,
    )
    assert result.ok is True
    heartbeats = [e for e in spy.events if e[0] == SUBAGENT_HEARTBEAT_EVENT]
    assert len(heartbeats) >= 1, f"expected >=1 heartbeat span event, got {spy.events}"
    # The heartbeat name is the span-event name, not a task_events CHECK value.
    assert SUBAGENT_HEARTBEAT_EVENT == "subagent.heartbeat"
    # The helper never reaches into the lease/heartbeat machinery.
    assert called["build_query"] == 0


def test_no_task_events_or_lease_symbols_in_module():
    src = Path(__file__).resolve().parents[1] / "executor" / "subagents" / "fanout.py"
    text = src.read_text()
    # Heartbeat must NOT be a task_events row nor a lease touch.
    assert "lease_expiry" not in text
    assert "build_heartbeat_query" not in text
    assert "HeartbeatManager" not in text
    assert "insert_cost_row" not in text


# --------------------------------------------------------------------------- #
# 8. Pattern B / cost forbidden symbols (grep-as-test)
# --------------------------------------------------------------------------- #
def test_no_pattern_b_symbols_in_module():
    src = Path(__file__).resolve().parents[1] / "executor" / "subagents" / "fanout.py"
    text = src.read_text()
    for forbidden in ("sub_agent_id", "parent_task_id", "waiting_for_subagent"):
        assert forbidden not in text, f"forbidden Pattern-B symbol present: {forbidden}"


def test_no_imperative_ainvoke_of_subgraph_in_module():
    """The sub-agent must be a Send-reached subgraph NODE, not an imperatively
    ainvoke'd subgraph. The helper drives the parent fan-out graph with
    durability='sync'; it must NOT call ``.ainvoke``/``.astream`` on a compiled
    *sub*-graph object inside a node/tool function."""
    src = Path(__file__).resolve().parents[1] / "executor" / "subagents" / "fanout.py"
    text = src.read_text()
    assert 'durability="sync"' in text
    # The fan-out is reached via Send.
    assert "Send(" in text


# --------------------------------------------------------------------------- #
# 9. Channel isolation — sub turns do not leak into parent ``messages``
# --------------------------------------------------------------------------- #
async def test_sub_messages_isolated_from_parent_messages():
    saver = _saver()
    model = FakeModel([
        _ai_tool_call("c1", "echo", {"text": "secret-internal"}),
        _ai_final("public summary only"),
    ])
    thread_id = "t-isolation"
    result = await run_subagent(
        prompt="do work then summarize",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=saver,
        thread_id=thread_id,
        emit=SpyEmit(),
    )
    assert result.ok is True

    # Inspect the top-level (parent) checkpoint: it must NOT carry the
    # sub-agent's internal working turns on a ``messages`` channel.
    cfg = {"configurable": {"thread_id": thread_id}}
    sub_ns_seen = False
    parent_leak = False
    async for ct in saver.alist(cfg):
        ns = ct.config["configurable"].get("checkpoint_ns", "")
        cv = ct.checkpoint.get("channel_values", {})
        if ns == "":
            # Parent level: must not contain the sub's internal channel/turns.
            for ch in ("sub_messages", "messages"):
                vals = cv.get(ch) or []
                for m in vals:
                    content = getattr(m, "content", "") or ""
                    if "secret-internal" in content or "echoed:secret-internal" in content:
                        parent_leak = True
        else:
            # Sub-checkpoint namespace: this is where the transcript lives.
            if "sub_messages" in cv:
                sub_ns_seen = True
    assert parent_leak is False, "sub-agent internal turns leaked into parent channels"
    assert sub_ns_seen is True, "sub-agent transcript was not persisted under a namespace"


# --------------------------------------------------------------------------- #
# 10. Failure is a return value, not an exception
# --------------------------------------------------------------------------- #
async def test_internal_error_returns_error_marker_not_raise():
    class BoomModel(FakeModel):
        async def ainvoke(self, messages, *args, **kwargs):
            raise RuntimeError("provider exploded")

    model = BoomModel([_ai_final("x")])
    result = await run_subagent(
        prompt="trigger error",
        tools=[_echo_tool()],
        ceiling=_ceiling(),
        depth=0,
        model=model,
        checkpointer=_saver(),
        thread_id="t-error",
        emit=SpyEmit(),
    )
    assert isinstance(result, SubagentResult)
    assert result.ok is False
    assert result.reason == "error"
