"""Unit tests for the Supervisor topology Scope node + state superset (Task S5).

All tests here are **fake-model, no-network, no-DB** — they exercise
``scope_node`` directly and through a tiny in-process ``MemorySaver`` graph
(``durability="sync"``, matching the worker runtime). No Postgres, no TCP
ports, no server subprocess → worktree-concurrency-safe.

Coverage maps 1:1 to the S5 acceptance criteria:

* state superset declares ``brief`` / ``iteration`` / ``subtasks`` /
  ``subagent_results`` (keyed reducer) / ``findings`` on top of every
  ``RuntimeState`` channel, each with the correct reducer;
* clear query → brief, no interrupt;
* ambiguous + ``scope_clarification_enabled=true`` → ``interrupt()`` (reusing
  the ``waiting_for_input`` machinery) then ``Command(resume=...)`` folds the
  answer into the brief;
* ambiguous + ``scope_clarification_enabled=false`` → never interrupts, produces
  a best-effort brief from the query alone;
* the brief is written exactly once on every path.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any, get_type_hints

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from executor.compaction.state import RuntimeState
from executor.supervisor import SupervisorState, scope_node
from executor.supervisor.state import _merge_subagent_results

# ``asyncio_mode = "auto"`` (pyproject.toml) runs async tests without a marker.


# --------------------------------------------------------------------------- #
# Fakes — a chat model driven by a scripted list of AIMessages.
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """Minimal injectable chat model that routes by prompt *kind*, not a counter.

    ``scope_node`` makes (at most) two ``ainvoke`` calls: the clarity
    assessment and the brief. On a clarify **resume** LangGraph re-executes the
    node from the top, so the assessment call fires *again* before the
    short-circuited ``interrupt()`` returns the resume value — a counter-based
    fake would hand the brief body to that replayed assessment call. Routing by
    a substring that only the brief prompt contains keeps the fake deterministic
    across replays (mirrors how a real model would return the same assessment).
    """

    _BRIEF_MARKER = "Write the research brief now"

    def __init__(self, assessment: str, brief: str):
        self._assessment = assessment
        self._brief = brief
        self.invocations = 0
        self.seen_prompts: list[Any] = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.invocations += 1
        self.seen_prompts.append(messages)
        text = messages if isinstance(messages, str) else str(messages)
        body = self._brief if self._BRIEF_MARKER in text else self._assessment
        return AIMessage(content=body)


def _assessment(*, clear: bool, question: str = "") -> str:
    return json.dumps({"clear": clear, "question": question})


def _config(*, model: ScriptedModel, scope_clarification_enabled: bool | None):
    supervisor: dict[str, Any] = {}
    if scope_clarification_enabled is not None:
        supervisor["scope_clarification_enabled"] = scope_clarification_enabled
    return {
        "configurable": {
            "thread_id": "scope-test",
            "scope_model": model,
            "agent_config": {"supervisor": supervisor},
        }
    }


def _build_graph() -> Any:
    g = StateGraph(SupervisorState)
    g.add_node("scope", scope_node)
    g.add_edge(START, "scope")
    g.add_edge("scope", END)
    return g.compile(checkpointer=MemorySaver())


# --------------------------------------------------------------------------- #
# State superset
# --------------------------------------------------------------------------- #
def test_supervisor_state_is_superset_of_runtime_state():
    rt_hints = get_type_hints(RuntimeState, include_extras=True)
    sup_hints = get_type_hints(SupervisorState, include_extras=True)
    # Every RuntimeState channel is present with the identical annotation.
    for name, hint in rt_hints.items():
        assert name in sup_hints, f"missing RuntimeState channel: {name}"
        assert sup_hints[name] == hint, f"annotation drift on {name}"
    # The supervisor additions are all present.
    for added in ("brief", "iteration", "subtasks", "subagent_results", "findings"):
        assert added in sup_hints, f"missing supervisor channel: {added}"


def test_supervisor_state_compiles_in_stategraph():
    # Builtin reducers like ``max`` fail LangGraph signature introspection; a
    # superset that compiles proves the named-reducer convention was followed.
    _build_graph()  # raises at construction if a channel annotation is bad


def test_subagent_results_reducer_is_keyed_and_idempotent():
    # Keyed by ``subtask`` (§A0 inv. 6): two writes for the same subtask key
    # collapse to one entry (idempotent re-delivery on redrive), distinct keys
    # accumulate.
    a = {"weather": {"summary": "first"}}
    b = {"weather": {"summary": "second"}, "tides": {"summary": "t"}}
    merged = _merge_subagent_results(a, b)
    assert merged == {"weather": {"summary": "second"}, "tides": {"summary": "t"}}
    # Re-applying the same delta is a no-op (idempotent).
    assert _merge_subagent_results(merged, b) == merged


def test_subagent_results_reducer_tolerates_empty_sides():
    assert _merge_subagent_results({}, {"k": {"summary": "v"}}) == {"k": {"summary": "v"}}
    assert _merge_subagent_results({"k": {"summary": "v"}}, {}) == {"k": {"summary": "v"}}


# --------------------------------------------------------------------------- #
# Clear query → brief, no interrupt
# --------------------------------------------------------------------------- #
async def test_clear_query_produces_brief_without_interrupt():
    model = ScriptedModel(_assessment(clear=True), "BRIEF: study the clear topic")
    graph = _build_graph()
    config = _config(model=model, scope_clarification_enabled=True)
    result = await graph.ainvoke(
        {"messages": [HumanMessage("a perfectly clear research question")]},
        config=config,
        durability="sync",
    )
    state = await graph.aget_state(config)
    assert not any(t.interrupts for t in state.tasks)  # no pause
    assert result["brief"] == "BRIEF: study the clear topic"
    assert result["iteration"] == 0
    assert result["subtasks"] == []
    assert result.get("subagent_results") == {}
    assert result.get("findings") == []


# --------------------------------------------------------------------------- #
# Ambiguous + flag-true → interrupt → resume folds the answer
# --------------------------------------------------------------------------- #
async def test_ambiguous_with_flag_true_interrupts_then_resumes_into_brief():
    model = ScriptedModel(_assessment(clear=False, question="Which region?"), "BRIEF: EU region focus")
    graph = _build_graph()
    config = _config(model=model, scope_clarification_enabled=True)

    # First pass pauses on interrupt() — no brief yet.
    first = await graph.ainvoke(
        {"messages": [HumanMessage("research the market")]},
        config=config,
        durability="sync",
    )
    state = await graph.aget_state(config)
    interrupts = [iv for t in state.tasks for iv in (t.interrupts or ())]
    assert interrupts, "expected an interrupt() pause"
    # Reuses the existing waiting_for_input payload shape: {"type": "input", ...}
    assert interrupts[0].value.get("type") == "input"
    assert "Which region?" in interrupts[0].value.get("prompt", "")
    assert "brief" not in first  # brief not written on the pausing pass

    # Resume with the human answer via the existing Command(resume=...) path.
    resumed = await graph.ainvoke(
        Command(resume="Europe"), config=config, durability="sync"
    )
    assert resumed["brief"] == "BRIEF: EU region focus"
    assert resumed["iteration"] == 0
    assert resumed["subtasks"] == []
    # The brief-generation prompt saw the folded-in answer.
    brief_prompt = model.seen_prompts[-1]
    flat = json.dumps(
        [getattr(m, "content", m) for m in brief_prompt]
        if isinstance(brief_prompt, list)
        else brief_prompt
    )
    assert "Europe" in flat


# --------------------------------------------------------------------------- #
# Ambiguous + flag-false → never interrupts, best-effort brief
# --------------------------------------------------------------------------- #
async def test_ambiguous_with_flag_false_never_interrupts():
    model = ScriptedModel(_assessment(clear=False, question="Which region?"), "BRIEF: best effort")
    graph = _build_graph()
    config = _config(model=model, scope_clarification_enabled=False)
    result = await graph.ainvoke(
        {"messages": [HumanMessage("research the market")]},
        config=config,
        durability="sync",
    )
    state = await graph.aget_state(config)
    assert not any(t.interrupts for t in state.tasks), "headless must never pause"
    assert result["brief"] == "BRIEF: best effort"
    assert result["iteration"] == 0
    assert result["subtasks"] == []


# --------------------------------------------------------------------------- #
# Brief write-once on every path
# --------------------------------------------------------------------------- #
async def test_brief_is_written_exactly_once_clear_path():
    model = ScriptedModel(_assessment(clear=True), "ONLY BRIEF")
    update = await scope_node(
        {"messages": [HumanMessage("clear q")]},
        _config(model=model, scope_clarification_enabled=True),
    )
    assert update["brief"] == "ONLY BRIEF"
    # The node returns the brief key exactly once in its update dict.
    assert list(update).count("brief") == 1


async def test_brief_is_written_exactly_once_headless_path():
    model = ScriptedModel(_assessment(clear=False, question="?"), "HEADLESS BRIEF")
    update = await scope_node(
        {"messages": [HumanMessage("vague q")]},
        _config(model=model, scope_clarification_enabled=False),
    )
    assert update["brief"] == "HEADLESS BRIEF"
    assert list(update).count("brief") == 1


# --------------------------------------------------------------------------- #
# Default when the flag is absent (preset does not seed it)
# --------------------------------------------------------------------------- #
async def test_flag_absent_defaults_to_clarify_enabled_on_ambiguous():
    # PresetDefaults leaves scope_clarification_enabled unset; the worker v1
    # default is "clarify on" (the feature's design intent). An ambiguous query
    # with the flag absent therefore interrupts.
    model = ScriptedModel(_assessment(clear=False, question="Need detail?"), "B")
    graph = _build_graph()
    config = _config(model=model, scope_clarification_enabled=None)
    await graph.ainvoke(
        {"messages": [HumanMessage("vague")]}, config=config, durability="sync"
    )
    state = await graph.aget_state(config)
    assert any(t.interrupts for t in state.tasks)
