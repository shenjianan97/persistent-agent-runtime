"""Each LLM-bearing Supervisor node surfaces its token spend (Task S8, §A11-E1).

The E1 cost mechanism requires every LLM-bearing supervisor node to accumulate
its ``usage_metadata`` into the ``step_usage`` channel it returns, so the parent
``execute_task`` cost loop can read it off the astream ``updates`` event and bill
it additively to the parent's super-step checkpoint. These are fake-model,
no-infra unit tests (worktree-safe).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from executor.supervisor import scope_node, supervisor_node
from executor.supervisor.nodes import writer_node


class UsageModel:
    """Fake chat model whose every response carries known usage_metadata."""

    # The brief prompt carries this marker (executor/supervisor/prompts.py); the
    # assessment prompt does not. Routing on it lets the fake return DISTINCT
    # bodies per call so the scope test exercises two real, separately-routed
    # LLM calls (not the same body twice).
    _BRIEF_MARKER = "Write the research brief now"

    def __init__(self, *, in_tok: int, out_tok: int, body: str = "{}"):
        self.in_tok = in_tok
        self.out_tok = out_tok
        self.body = body
        self.calls = 0

    async def ainvoke(self, messages, *a, **k):
        self.calls += 1
        text = messages if isinstance(messages, str) else str(messages)
        # scope_node routes: assessment (JSON) then brief (prose). Other nodes
        # ignore the marker and get ``self.body`` for every call.
        if self._BRIEF_MARKER in text:
            body = "Research brief."
        else:
            body = self.body
        return AIMessage(
            content=body,
            usage_metadata={
                "input_tokens": self.in_tok,
                "output_tokens": self.out_tok,
                "total_tokens": self.in_tok + self.out_tok,
            },
        )


async def test_scope_node_surfaces_step_usage():
    model = UsageModel(in_tok=100, out_tok=50, body=json.dumps({"clear": True}))
    config = {
        "configurable": {
            "thread_id": "t",
            "scope_model": model,
            "agent_config": {"supervisor": {"scope_clarification_enabled": False}},
        }
    }
    state = {"messages": [HumanMessage(content="research topic X")]}
    out = await scope_node(state, config)
    # scope makes 2 distinct LLM calls (assessment JSON + brief prose); their
    # usage is SUMMED into step_usage → 200 in / 100 out.
    assert model.calls == 2
    assert out["step_usage"]["input_tokens"] == 200
    assert out["step_usage"]["output_tokens"] == 100


async def test_supervisor_node_surfaces_step_usage():
    decision = json.dumps(
        {"decision": "stop", "subtasks": [], "reason": "done"}
    )
    model = UsageModel(in_tok=70, out_tok=30, body=decision)
    config = {
        "configurable": {
            "thread_id": "t",
            "supervisor_model": model,
            "agent_config": {"supervisor": {}},
        }
    }
    state = {"brief": "b", "iteration": 0, "subagent_results": {}}
    out = await supervisor_node(state, config)
    assert out["step_usage"]["input_tokens"] == 70
    assert out["step_usage"]["output_tokens"] == 30


async def test_writer_node_surfaces_step_usage():
    # Writer makes one writer call + (per cited finding) verify calls. With no
    # findings there are no verify calls, so only the one writer call's usage.
    model = UsageModel(in_tok=500, out_tok=200, body="A report with no citations.")
    config = {
        "configurable": {
            "thread_id": "t",
            "writer_model": model,
            "verify_model": model,
            "agent_config": {"supervisor": {"writer_style": "formal_report"}},
        }
    }
    state: dict[str, Any] = {"brief": "b", "findings": []}
    out = await writer_node(state, config)
    assert out["step_usage"]["input_tokens"] == 500
    assert out["step_usage"]["output_tokens"] == 200
