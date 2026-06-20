"""Unit tests for the Supervisor cost-attribution helper (Task S8, §A11-E1).

These cover the pure usage-accumulation helper that lets every LLM-bearing
Supervisor node (scope / supervisor / writer / verify) and the fan-out
sub-agents surface their token spend through the ``step_usage`` state channel.
The ``execute_task`` cost loop reads that channel per super-step and writes it
ADDITIVELY to the parent's super-step ``checkpoint_id`` — the E1 mechanism.

No infra: these are pure functions over fake AIMessages, worktree-safe.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from executor.supervisor.cost import (
    UsageAccumulatingModel,
    merge_step_usage,
    usage_from_message,
    usage_from_messages,
)


def _msg(inp: int, out: int, **extra) -> AIMessage:
    um = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    um.update(extra)
    return AIMessage(content="x", usage_metadata=um)


def test_usage_from_message_extracts_input_output():
    u = usage_from_message(_msg(100, 50))
    assert u == {"input_tokens": 100, "output_tokens": 50}


def test_usage_from_message_carries_cache_counters_when_present():
    msg = _msg(
        100,
        50,
        input_token_details={"cache_creation": 20, "cache_read": 30},
    )
    u = usage_from_message(msg)
    assert u["input_tokens"] == 100
    assert u["output_tokens"] == 50
    assert u["cache_creation_input_tokens"] == 20
    assert u["cache_read_input_tokens"] == 30


def test_usage_from_message_no_usage_metadata_is_empty():
    assert usage_from_message(AIMessage(content="x")) == {}


def test_usage_from_messages_sums_a_list():
    u = usage_from_messages([_msg(100, 50), _msg(10, 5)])
    assert u == {"input_tokens": 110, "output_tokens": 55}


def test_merge_step_usage_is_additive_per_field():
    a = {"input_tokens": 100, "output_tokens": 50}
    b = {"input_tokens": 200, "output_tokens": 100, "cache_read_input_tokens": 7}
    merged = merge_step_usage(a, b)
    assert merged == {
        "input_tokens": 300,
        "output_tokens": 150,
        "cache_read_input_tokens": 7,
    }


def test_merge_step_usage_handles_none_and_empty():
    assert merge_step_usage(None, None) == {}
    assert merge_step_usage({"input_tokens": 5}, None) == {"input_tokens": 5}
    assert merge_step_usage(None, {"output_tokens": 9}) == {"output_tokens": 9}


def test_merge_step_usage_never_mutates_inputs():
    a = {"input_tokens": 1}
    b = {"input_tokens": 2}
    merge_step_usage(a, b)
    assert a == {"input_tokens": 1}
    assert b == {"input_tokens": 2}


# --------------------------------------------------------------------------- #
# UsageAccumulatingModel — captures usage of LLM calls made INSIDE a callee
# (e.g. citations.verify) that would otherwise discard it.
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, in_tok, out_tok):
        self.in_tok = in_tok
        self.out_tok = out_tok
        self.calls = 0

    async def ainvoke(self, *a, **k):
        self.calls += 1
        return AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": self.in_tok,
                "output_tokens": self.out_tok,
                "total_tokens": self.in_tok + self.out_tok,
            },
        )


async def test_usage_accumulating_model_sums_each_call():
    inner = _FakeModel(40, 20)
    wrapped = UsageAccumulatingModel(inner)
    await wrapped.ainvoke("a")
    await wrapped.ainvoke("b")
    assert inner.calls == 2
    assert wrapped.usage == {"input_tokens": 80, "output_tokens": 40}


async def test_usage_accumulating_model_passes_through_response():
    inner = _FakeModel(1, 1)
    wrapped = UsageAccumulatingModel(inner)
    resp = await wrapped.ainvoke("x")
    assert resp.content == "ok"
