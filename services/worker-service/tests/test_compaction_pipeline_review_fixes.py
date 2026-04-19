"""Regression tests for the three Codex review fixes on PR #80.

P1 — Tier 3 summariser receives a real cost-ledger adapter (not None), so a
     single firing does NOT flip ``tier3_fatal_short_circuited`` to True and
     disable Tier 3 for the rest of the task.

P2a — ``last_super_step_message_count`` tracks the length of PERSISTED
      messages (``state["messages"]``), not the possibly system-prompt-
      prepended ``raw_messages`` handed to the pipeline.  Without this, the
      next super-step's conversation-log slice starts too far ahead and
      silently drops newly-added user/tool entries.

P2b — When the pre-Tier-3 flush hook fires, the hard-floor check is
      recomputed against ``[*messages, flush_message]`` so the appended
      SystemMessage's tokens can still trigger ``HardFloorEvent`` instead of
      the next LLM call hitting a provider context-limit error.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.defaults import KEEP_TOOL_USES
from executor.compaction.pipeline import (
    CompactionPassResult,
    HardFloorEvent,
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compact_for_llm,
)
from executor.compaction.summarizer import SummarizeResult
from executor.compaction.thresholds import resolve_thresholds


# ---------------------------------------------------------------------------
# Shared helpers — narrower than the Task 8 pipeline suite so the review
# fixes can be exercised in isolation.
# ---------------------------------------------------------------------------


def _tool_pair(i: int, body: int = 20) -> list[BaseMessage]:
    call_id = f"call_{i}"
    return [
        AIMessage(
            content=f"Step {i}",
            tool_calls=[{
                "id": call_id,
                "name": f"tool_{i}",
                "args": {"content": "x" * 10},
                "type": "tool_call",
            }],
        ),
        ToolMessage(content="r" * body, tool_call_id=call_id, name=f"tool_{i}"),
    ]


def _messages(n: int, body: int = 20) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n):
        msgs.extend(_tool_pair(i, body=body))
    return msgs


def _state(**overrides) -> dict[str, Any]:
    s = {
        "cleared_through_turn_index": 0,
        "truncated_args_through_turn_index": 0,
        "summarized_through_turn_index": 0,
        "summary_marker": "",
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 0,
        "tier3_fatal_short_circuited": False,
    }
    s.update(overrides)
    return s


def _agent_config(**overrides) -> dict[str, Any]:
    cfg = {"provider": "other", "model": "test-model", "context_management": {}}
    cfg.update(overrides)
    return cfg


def _task_context(cost_ledger: Any = None, **overrides) -> dict[str, Any]:
    ctx = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "checkpoint_id": None,
        "cost_ledger": cost_ledger,
        "callbacks": [],
    }
    ctx.update(overrides)
    return ctx


def _fixed_tokens(value: int):
    return lambda msgs: value


class _RecordingLedger:
    """Minimal ledger stub matching the summariser's protocol.

    Records every insert call so tests can assert Tier 3 actually wrote a
    row instead of being silently dropped into the fatal short-circuit path.
    """

    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []

    async def insert(self, **kwargs: Any) -> None:
        self.inserts.append(kwargs)


def _successful_summarizer(*, summary: str = "Summary of earlier turns."):
    async def _summarize(**_kwargs):
        return SummarizeResult(
            summary_text=summary,
            skipped=False,
            skipped_reason=None,
            summarizer_model_id="test-summarizer",
            tokens_in=100,
            tokens_out=20,
            cost_microdollars=0,
            latency_ms=12,
        )

    return _summarize


# ---------------------------------------------------------------------------
# P1 — Tier 3 with a real ledger adapter does not short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_with_ledger_adapter_does_not_short_circuit() -> None:
    """Regression for Codex P1:

    Before the fix, ``task_context["cost_ledger"]`` was None.  `summarize_slice`
    caught the resulting AttributeError as "fatal" and set
    ``tier3_fatal_short_circuited=True`` forever.  The adapter wired in PR #80
    follow-up keeps Tier 3 live across the whole task.
    """
    msgs = _messages(10, body=200)  # enough to push over thresholds
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)
    # Force Tier 3: far above tier3 threshold, Tier-1 savings insufficient.
    est = thresholds.tier3 + 1_500

    ledger = _RecordingLedger()

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_state(),
        agent_config=_agent_config(),
        model_context_window=model_context_window,
        task_context=_task_context(cost_ledger=ledger),
        summarizer=_successful_summarizer(),
        estimate_tokens_fn=_fixed_tokens(est),
    )

    assert isinstance(result, CompactionPassResult)
    # tier3_fatal_short_circuited MUST NOT be set — that is the bug's signature.
    assert result.state_updates.get("tier3_fatal_short_circuited") is not True
    # A Tier 3 firing occurred and the summary_marker was updated.
    tier3 = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    skipped = [e for e in result.events if isinstance(e, Tier3SkippedEvent)]
    assert tier3, f"expected Tier 3 firing; got events={result.events!r}"
    assert not skipped, f"Tier 3 was wrongly skipped: {skipped!r}"


# ---------------------------------------------------------------------------
# P2a — watermark tracks state["messages"], not raw_messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watermark_uses_state_messages_not_raw_messages() -> None:
    """Regression for Codex P2a:

    Caller prepends 2 transient SystemMessages to the ``raw_messages`` arg
    for token estimation, but ``state["messages"]`` only stores the
    persisted 5 messages.  Watermark MUST be 5, not 7 — otherwise the next
    super-step slices ``state["messages"][7:]`` which is empty and drops
    the new user/tool entries silently.
    """
    persisted = _messages(2)  # 5 messages: HumanMessage + 2 tool pairs
    transient_system_prompts = [
        SystemMessage(content="system prompt 1"),
        SystemMessage(content="system prompt 2"),
    ]
    raw_messages = transient_system_prompts + persisted

    result = await compact_for_llm(
        raw_messages=raw_messages,
        state=_state(messages=persisted),
        agent_config=_agent_config(),
        model_context_window=100_000,
        task_context=_task_context(),
        summarizer=_successful_summarizer(),
        estimate_tokens_fn=_fixed_tokens(100),  # below Tier 1
    )

    assert result.state_updates["last_super_step_message_count"] == len(persisted)
    assert result.state_updates["last_super_step_message_count"] != len(raw_messages)


@pytest.mark.asyncio
async def test_watermark_falls_back_to_raw_messages_when_state_missing() -> None:
    """Backward compat: if state has no 'messages' key (old tests, early
    super-steps before the reducer materializes), watermark falls back to
    ``len(raw_messages)`` — preserves prior behaviour for those paths.
    """
    msgs = _messages(2)
    state_without_messages = _state()
    assert "messages" not in state_without_messages

    result = await compact_for_llm(
        raw_messages=msgs,
        state=state_without_messages,
        agent_config=_agent_config(),
        model_context_window=100_000,
        task_context=_task_context(),
        summarizer=_successful_summarizer(),
        estimate_tokens_fn=_fixed_tokens(100),
    )

    assert result.state_updates["last_super_step_message_count"] == len(msgs)


# ---------------------------------------------------------------------------
# P2b — hard-floor recomputed after flush message is appended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_tier3_flush_hard_floor_recomputed_with_flush_message() -> None:
    """Regression for Codex P2b:

    Pre-flush ``est_tokens`` is just under ``model_context_window``.  The
    flush SystemMessage adds enough extra tokens to push the full view over
    the limit.  Before the fix, the hard-floor check used the stale pre-
    flush estimate and never emitted ``HardFloorEvent``; the next LLM call
    would then fail with a provider context-limit error instead of taking
    the explicit dead-letter path.
    """
    # Enough tool messages so Tier 3 would fire, with memory.enabled so
    # should_fire_pre_tier3_flush returns True on first crossing.
    msgs = _messages(20, body=50)
    model_context_window = 10_000
    thresholds = resolve_thresholds(model_context_window)

    # est_tokens just under the hard floor BUT over Tier 3 threshold so the
    # flush path is taken.  After appending the flush message, tokens
    # exceed the floor.
    under_floor = model_context_window - 5
    over_floor = model_context_window + 50

    call_counter = {"n": 0}

    def _varying_tokens(messages: list[BaseMessage]) -> int:
        call_counter["n"] += 1
        # First call is the Step-1 pre-tier estimate (drives tier routing).
        # Subsequent call(s) evaluate the flush-inclusive view.
        return under_floor if call_counter["n"] == 1 else over_floor

    agent_config = _agent_config(memory={"enabled": True})

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_state(memory_flush_fired_this_task=False),
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_successful_summarizer(),
        estimate_tokens_fn=_varying_tokens,
    )

    # The flush should have fired (memory_flush_fired_this_task → True).
    assert result.state_updates.get("memory_flush_fired_this_task") is True
    assert any(isinstance(e, MemoryFlushFiredEvent) for e in result.events)
    # Critically: HardFloorEvent MUST be present, carrying the RECOMPUTED
    # est_tokens (over the floor), not the pre-flush estimate.
    floor_events = [e for e in result.events if isinstance(e, HardFloorEvent)]
    assert floor_events, (
        "HardFloorEvent missing — recompute-after-flush regression. "
        f"Events were: {result.events!r}"
    )
    assert floor_events[0].est_tokens > model_context_window
    assert floor_events[0].est_tokens == over_floor


@pytest.mark.asyncio
async def test_pre_tier3_flush_no_hard_floor_when_flush_stays_under_limit() -> None:
    """Complementary: if flush_view tokens stay under the context window,
    no HardFloorEvent should be emitted and the caller proceeds to the
    LLM call with the flush appended.
    """
    msgs = _messages(20, body=50)
    model_context_window = 10_000

    call_counter = {"n": 0}

    def _varying_tokens(messages: list[BaseMessage]) -> int:
        call_counter["n"] += 1
        # Both estimates stay safely under the window.
        return 9_000 if call_counter["n"] == 1 else 9_200

    agent_config = _agent_config(memory={"enabled": True})

    result = await compact_for_llm(
        raw_messages=msgs,
        state=_state(memory_flush_fired_this_task=False),
        agent_config=agent_config,
        model_context_window=model_context_window,
        task_context=_task_context(),
        summarizer=_successful_summarizer(),
        estimate_tokens_fn=_varying_tokens,
    )

    assert result.state_updates.get("memory_flush_fired_this_task") is True
    assert not any(isinstance(e, HardFloorEvent) for e in result.events)
