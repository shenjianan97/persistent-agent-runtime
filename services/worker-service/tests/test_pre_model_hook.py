"""Track 7 Follow-up (Task 3) — ``compaction_pre_model_hook`` acceptance tests.

Maps to the acceptance criteria in
``docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/
task-3-pre-model-hook-architecture.md``.

These tests use the deterministic ``estimate_tokens_fn`` injection point so
thresholds can be exercised without a real tokenizer, and ``AsyncMock``
summarisers so assertions can be made about the raw middle messages passed in.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from structlog.testing import capture_logs

from executor.compaction.pre_model_hook import (
    HardFloorEvent,
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compaction_pre_model_hook,
    find_keep_window_start,
)
from executor.compaction.summarizer import SummarizeResult
from tests.shape_validator import assert_valid_shape


# NOTE: ``capture_logs`` rebinds the structlog logger and bypasses the level
# filter set by ``core.logging.configure_logging``, so DEBUG assertions here
# pass regardless of ``WORKER_LOG_LEVEL``. That is the intended behaviour —
# tests assert emission, not filter-layer suppression. If you're reading this
# and concluding "DEBUG is on by default," it isn't: production runs are still
# gated by ``WORKER_LOG_LEVEL`` at the bind layer.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tool_pair(i: int, result_content: str | None = None) -> list[BaseMessage]:
    call_id = f"call_{i}"
    content = result_content if result_content is not None else f"result {i}"
    return [
        AIMessage(
            content=f"Step {i}",
            tool_calls=[
                {
                    "id": call_id,
                    "name": f"tool_{i}",
                    "args": {"content": "x" * 10},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=content, tool_call_id=call_id, name=f"tool_{i}"),
    ]


def _build_messages(n_pairs: int, large_result_bytes: int | None = None) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [HumanMessage(content="task input")]
    for i in range(n_pairs):
        content = ("x" * large_result_bytes) if large_result_bytes else f"result {i}"
        msgs.extend(_tool_pair(i, result_content=content))
    return msgs


def _fresh_state(messages: list[BaseMessage]) -> dict[str, Any]:
    return {
        "messages": messages,
        "summary": "",
        "summarized_through_turn_index": 0,
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 0,
        "tier3_fatal_short_circuited": False,
    }


def _agent_config(
    *,
    memory_enabled: bool = False,
    pre_flush: bool = True,
    context_management: dict | None = None,
) -> dict[str, Any]:
    ctx_mgmt = dict(context_management or {})
    ctx_mgmt.setdefault("pre_tier3_memory_flush", pre_flush)
    return {
        "provider": "other",
        "model": "test-model",
        "memory": {"enabled": memory_enabled},
        "context_management": ctx_mgmt,
    }


def _task_context() -> dict[str, Any]:
    return {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "checkpoint_id": None,
        "cost_ledger": None,
        "callbacks": [],
    }


def _fixed_estimator(tokens: int) -> Callable[[list[BaseMessage]], int]:
    def _e(_messages: list[BaseMessage]) -> int:
        return tokens

    return _e


def _make_summarizer(
    *, summary_text: str = "NEW SUMMARY", skipped: bool = False,
    skipped_reason: str | None = None,
) -> AsyncMock:
    mock = AsyncMock()
    mock.return_value = SummarizeResult(
        summary_text=summary_text if not skipped else None,
        skipped=skipped,
        skipped_reason=skipped_reason,
        summarizer_model_id="test-model",
        tokens_in=100,
        tokens_out=50,
        cost_microdollars=0,
        latency_ms=10,
    )
    return mock


# ---------------------------------------------------------------------------
# AC-1 — compact_for_llm is gone, pre_model_hook is wired
# ---------------------------------------------------------------------------


def test_compact_for_llm_symbol_gone():
    """Track 7 Follow-up removed ``compact_for_llm``; importing it must fail."""
    with pytest.raises(ImportError):
        from executor.compaction.pipeline import compact_for_llm  # noqa: F401


def test_graph_wires_pre_model_hook():
    """``agent_node`` must call ``compaction_pre_model_hook`` and not ``compact_for_llm``."""
    graph_path = pathlib.Path(__file__).parent.parent / "executor" / "graph.py"
    src = graph_path.read_text()
    assert "compaction_pre_model_hook" in src, (
        "executor/graph.py must invoke compaction_pre_model_hook from agent_node."
    )
    # No non-docstring / comment reference to the deleted symbol should remain
    # as a call — simple sanity: there is no ``compact_for_llm(`` anywhere.
    assert "compact_for_llm(" not in src, (
        "executor/graph.py still contains a call to the removed compact_for_llm."
    )


# ---------------------------------------------------------------------------
# AC-3 — three-region projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_region_order():
    """Projection order: [SystemMessage(prompt), SystemMessage(summary)?, *middle, *keep]."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)
    state["summary"] = "prior summary"
    state["summarized_through_turn_index"] = 1  # HumanMessage is "summarised"

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(),
        estimate_tokens_fn=_fixed_estimator(1_000),  # below threshold — no firing
        system_prompt="SYSTEM PROMPT",
    )

    projection = result.messages
    assert isinstance(projection[0], SystemMessage)
    assert projection[0].content == "SYSTEM PROMPT"
    assert isinstance(projection[1], SystemMessage)
    assert projection[1].content == "prior summary"
    # Content after head is non-system.
    for m in projection[2:]:
        assert not isinstance(m, SystemMessage)


@pytest.mark.asyncio
async def test_projection_omits_summary_when_empty():
    """When ``state.summary`` is empty, no summary SystemMessage is inserted."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(),
        estimate_tokens_fn=_fixed_estimator(1_000),
        system_prompt="SYS",
    )

    # Only ONE SystemMessage at head (the prompt).
    sys_at_head = 0
    for m in result.messages:
        if isinstance(m, SystemMessage):
            sys_at_head += 1
        else:
            break
    assert sys_at_head == 1, (
        "When summary is empty, only the system-prompt SystemMessage should "
        f"appear at the head; found {sys_at_head}."
    )


# ---------------------------------------------------------------------------
# AC-4 / AC-5 — summariser receives RAW middle; main LLM sees no stubs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarizer_receives_raw_middle():
    """Firing turn: mock summariser must receive the raw middle messages —
    never a stubbed / placeholder ``ToolMessage`` content.
    """
    # 10 tool results, each 8KB — Track 7 would have stubbed the older ones.
    msgs = _build_messages(n_pairs=10, large_result_bytes=8_000)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer()
    await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),  # above 0.85 × 10K
    )

    summarizer.assert_awaited_once()
    call_kwargs = summarizer.await_args.kwargs
    middle: list[BaseMessage] = call_kwargs["slice_messages"]
    # Every ToolMessage in the slice must carry raw content, not a stub.
    stub_marker = "[tool output not retained"
    for m in middle:
        if isinstance(m, ToolMessage):
            assert isinstance(m.content, str)
            assert not m.content.startswith(stub_marker), (
                "Summariser received a stubbed ToolMessage — Track 7 Follow-up "
                "requires RAW middle content."
            )
    # prior_summary carried (empty on first firing) + summarizer_context_window.
    assert "prior_summary" in call_kwargs
    assert "summarizer_context_window" in call_kwargs


@pytest.mark.asyncio
async def test_main_llm_sees_no_stubs():
    """After the hook returns, the projection contains no ``[tool output not
    retained …]`` stubs — post-firing it's summary + keep_window; below
    threshold it's raw middle + keep_window.
    """
    msgs = _build_messages(n_pairs=10, large_result_bytes=8_000)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(summary_text="rich summary"),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )

    stub_marker = "[tool output not retained"
    for m in result.messages:
        if isinstance(m, (ToolMessage, SystemMessage)):
            content = getattr(m, "content", "")
            if isinstance(content, str):
                assert not content.startswith(stub_marker)


# ---------------------------------------------------------------------------
# AC-6 — replace semantics, journal not mutated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_firing_state_replace_semantics():
    """After a firing, ``summary`` is REPLACED (not appended) and
    ``summarized_through_turn_index`` advances to ``keep_window_start``.
    """
    msgs = _build_messages(n_pairs=6)
    state = _fresh_state(msgs)
    state["summary"] = "OLD SUMMARY — this should vanish"

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(summary_text="FRESH SUMMARY"),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )

    # Replace, not append.
    assert result.state_updates["summary"] == "FRESH SUMMARY"
    assert "OLD SUMMARY" not in result.state_updates["summary"]

    # Watermark advanced to the keep_window_start.
    expected_start = find_keep_window_start(msgs)
    assert (
        result.state_updates["summarized_through_turn_index"] == expected_start
    )

    # tier3_firings_count incremented.
    assert result.state_updates["tier3_firings_count"] == 1


@pytest.mark.asyncio
async def test_journal_not_mutated_on_firing():
    """The hook must treat ``raw_messages`` as read-only."""
    msgs = _build_messages(n_pairs=6)
    snapshot = list(msgs)
    state = _fresh_state(msgs)

    await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )

    # Same list object, same contents.
    assert msgs == snapshot
    for a, b in zip(msgs, snapshot):
        assert a is b


@pytest.mark.asyncio
async def test_journal_append_only_across_turns():
    """Across multiple hook invocations, ``state["messages"]`` grows via
    append only — the hook never produces a ``messages`` state update that
    would rewrite a prior entry.
    """
    msgs = _build_messages(n_pairs=3)
    for turn in range(3):
        state = _fresh_state(msgs)
        result = await compaction_pre_model_hook(
            raw_messages=msgs,
            state=state,
            agent_config=_agent_config(),
            model_context_window=10_000,
            task_context=_task_context(),
            summarizer=_make_summarizer(),
            estimate_tokens_fn=_fixed_estimator(9_500),
        )
        assert "messages" not in result.state_updates, (
            "Task 3's hook must never write back to state['messages']; "
            "that mutation belongs to the recall-pointer rewrite path."
        )
        # Append a turn to simulate further agent progress.
        msgs = msgs + _tool_pair(100 + turn)


# ---------------------------------------------------------------------------
# AC-7 — trigger fraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_at_or_above_compaction_fraction():
    """Summarisation fires when ``est_tokens >= 0.85 * model_context_window``."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer()
    # 0.85 × 10_000 = 8_500. At 8_500, the hook must fire.
    await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(8_500),
    )
    summarizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_summarizer_below_threshold():
    """Below 0.85 × context_window the summariser is NOT invoked."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer()
    with capture_logs() as caps:
        result = await compaction_pre_model_hook(
            raw_messages=msgs,
            state=state,
            agent_config=_agent_config(),
            model_context_window=10_000,
            task_context=_task_context(),
            summarizer=summarizer,
            estimate_tokens_fn=_fixed_estimator(8_499),
        )
    summarizer.assert_not_awaited()
    # No firing means no summary / watermark updates.
    assert "summary" not in result.state_updates
    assert "summarized_through_turn_index" not in result.state_updates
    # Firings count never advances without a firing.
    assert "tier3_firings_count" not in result.state_updates
    # Exactly one projection trace with the expected outcome + shape.
    traces = [e for e in caps if e.get("event") == "compaction.projection_built"]
    assert len(traces) == 1, f"expected one projection trace, got {traces}"
    (trace,) = traces
    assert trace["outcome"] == "below_threshold"
    assert trace["est_tokens"] == 8_499
    assert trace["trigger_tokens"] == 8_500
    assert trace["model_context_window"] == 10_000
    assert trace["task_id"] == "task-1"
    assert trace["tenant_id"] == "tenant-1"
    assert trace["agent_id"] == "agent-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["below_threshold", "fired", "cap_reached", "fatal_short_circuit", "empty_summary"],
)
async def test_projection_trace_emitted_once_per_call(scenario: str) -> None:
    """Every return path in ``compaction_pre_model_hook`` emits exactly one
    ``compaction.projection_built`` DEBUG trace, carrying the correct outcome
    tag. Guards the emit-once invariant across the 9 return sites funnelled
    through ``_finalize``."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)
    summarizer = _make_summarizer()

    if scenario == "below_threshold":
        est = 8_499  # < 0.85 * 10_000
        expected_outcome = "below_threshold"
    elif scenario == "fired":
        est = 9_000  # ≥ trigger; summarizer returns a valid summary
        expected_outcome = "fired"
    elif scenario == "cap_reached":
        est = 9_000
        state["tier3_firings_count"] = 10  # hit TIER_3_MAX_FIRINGS_PER_TASK
        expected_outcome = "skipped:cap_reached"
    elif scenario == "fatal_short_circuit":
        est = 9_000
        state["tier3_fatal_short_circuited"] = True
        expected_outcome = "fatal_short_circuit"
    elif scenario == "empty_summary":
        est = 9_000
        summarizer = _make_summarizer(summary_text="   ")  # whitespace-only
        expected_outcome = "skipped:empty_summary"
    else:  # pragma: no cover
        raise AssertionError(scenario)

    with capture_logs() as caps:
        await compaction_pre_model_hook(
            raw_messages=msgs,
            state=state,
            agent_config=_agent_config(),
            model_context_window=10_000,
            task_context=_task_context(),
            summarizer=summarizer,
            estimate_tokens_fn=_fixed_estimator(est),
        )

    traces = [e for e in caps if e.get("event") == "compaction.projection_built"]
    assert len(traces) == 1, (
        f"scenario={scenario}: expected exactly one trace per hook call, got "
        f"{len(traces)}: {traces}"
    )
    (trace,) = traces
    assert trace["outcome"] == expected_outcome, (
        f"scenario={scenario}: expected outcome={expected_outcome}, got {trace!r}"
    )
    assert trace["log_level"] == "debug"


def test_all_returns_funnel_through_finalize() -> None:
    """Lint-style guard: the only ``return CompactionPassResult(`` in
    ``pre_model_hook.py`` must be inside ``_finalize`` itself — every caller
    site must go through ``_finalize(...)`` so the DEBUG trace fires exactly
    once. If this test fails, a new return site was added without routing it
    through ``_finalize``.
    """
    src_path = (
        pathlib.Path(__file__).parent.parent
        / "executor"
        / "compaction"
        / "pre_model_hook.py"
    )
    src = src_path.read_text()
    # Count non-``_finalize`` return sites by excluding the one inside the
    # helper. We don't parse — the structural guard is "at most one raw
    # return of CompactionPassResult exists in the file, and it lives inside
    # _finalize". Exact count is 1.
    raw_returns = src.count("return CompactionPassResult(")
    assert raw_returns == 1, (
        f"Expected exactly 1 'return CompactionPassResult(' (inside _finalize); "
        f"found {raw_returns}. New return sites must call _finalize(...) so "
        f"the compaction.projection_built DEBUG trace emits once per invocation."
    )


# ---------------------------------------------------------------------------
# AC-8 — keep-window orphan alignment
# ---------------------------------------------------------------------------


def test_keep_window_orphan_alignment():
    """The keep window start must land on an AIMessage with tool_calls.

    Regression from PR #80: walking back 3 ToolMessages from a dense tool-
    pair layout lands on a ToolMessage whose matching AIMessage is one step
    earlier; the orphan-alignment step is what prevents a leading orphan.
    """
    msgs = _build_messages(n_pairs=6)  # HumanMessage + 6 AI/Tool pairs = 13 msgs
    start = find_keep_window_start(msgs, keep=3)
    assert isinstance(msgs[start], AIMessage)
    assert msgs[start].tool_calls, (
        "keep_window_start must land on an AIMessage with tool_calls so the "
        "paired ToolMessages are not orphaned."
    )
    # Slice from start to end is a valid shape.
    assert_valid_shape(msgs[start:])


def test_keep_window_with_few_tools_returns_zero():
    """With fewer than KEEP_TOOL_USES tool messages, keep_window_start is 0."""
    msgs: list[BaseMessage] = [HumanMessage(content="start")]
    # Only two tool pairs — below KEEP_TOOL_USES=3.
    msgs.extend(_tool_pair(0))
    msgs.extend(_tool_pair(1))
    assert find_keep_window_start(msgs, keep=3) == 0


# ---------------------------------------------------------------------------
# AC-9 — pre-summarisation memory flush preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_flush_fires_when_all_conditions_hold():
    """Flush fires on a non-heartbeat turn when memory is enabled + flag on."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)
    # last_super_step_message_count is 0 → not a heartbeat turn.

    summarizer = _make_summarizer()
    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(memory_enabled=True, pre_flush=True),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    # Flush defers summarisation to the next turn.
    summarizer.assert_not_awaited()

    # MemoryFlushFiredEvent emitted + state flag set.
    assert any(isinstance(e, MemoryFlushFiredEvent) for e in result.events)
    assert result.state_updates.get("memory_flush_fired_this_task") is True

    # Flush message appended at end — must NOT be a SystemMessage because
    # langchain_anthropic rejects non-consecutive system messages. Carries
    # the ``pre_tier3_memory_flush`` marker in ``additional_kwargs`` so
    # downstream consumers can still detect the flush turn.
    last = result.messages[-1]
    assert not isinstance(last, SystemMessage), (
        "flush tail must not be a SystemMessage — see "
        "test_memory_flush_projection_has_no_tail_system_message"
    )
    assert last.additional_kwargs.get("compaction_event") == "pre_tier3_memory_flush"


@pytest.mark.asyncio
async def test_memory_flush_does_not_fire_twice():
    """One-shot: when ``memory_flush_fired_this_task`` is True, the flush does
    NOT re-fire. The summariser runs instead.
    """
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)
    state["memory_flush_fired_this_task"] = True

    summarizer = _make_summarizer()
    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(memory_enabled=True, pre_flush=True),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    summarizer.assert_awaited_once()
    assert not any(isinstance(e, MemoryFlushFiredEvent) for e in result.events)


@pytest.mark.asyncio
async def test_memory_flush_skipped_when_memory_disabled():
    """Memory-disabled agents never flush — summariser fires directly."""
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer()
    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(memory_enabled=False, pre_flush=True),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    summarizer.assert_awaited_once()
    assert not any(isinstance(e, MemoryFlushFiredEvent) for e in result.events)


# ---------------------------------------------------------------------------
# AC-11 — summarizer_context_window forwarded (chunking integration surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarizer_context_window_forwarded():
    """``summarizer_context_window`` kwarg reaches the summariser so Task 2's
    recursive chunking can engage when the middle is oversized.
    """
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)
    summarizer = _make_summarizer()

    await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),
        summarizer_context_window=200_000,
    )
    assert summarizer.await_args.kwargs["summarizer_context_window"] == 200_000


# ---------------------------------------------------------------------------
# AC-12 — hard-floor dead-letter path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_floor_event_emitted_when_over_window():
    """If even after summarisation the projection exceeds the context window,
    the hook emits a ``HardFloorEvent`` so the caller can dead-letter.
    """
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    # Estimator returns a huge value irrespective of projection shape, so the
    # post-summarisation re-estimate is still over the window.
    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(summary_text="SHORT"),
        estimate_tokens_fn=_fixed_estimator(50_000),
    )
    assert any(isinstance(e, HardFloorEvent) for e in result.events)


# ---------------------------------------------------------------------------
# AC-13 — single firing per invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_firing_per_invocation():
    """The summariser is called at most once per hook invocation."""
    msgs = _build_messages(n_pairs=6)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer()
    await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    assert summarizer.await_count == 1


# ---------------------------------------------------------------------------
# Bonus — skipped / fatal / cap paths surface as events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fatal_summarizer_sets_short_circuit_flag():
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(skipped=True, skipped_reason="fatal"),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    assert result.state_updates.get("tier3_fatal_short_circuited") is True
    assert any(
        isinstance(e, Tier3SkippedEvent) and e.reason == "fatal"
        for e in result.events
    )


@pytest.mark.asyncio
async def test_retryable_summarizer_preserves_watermark():
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(skipped=True, skipped_reason="retryable"),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    # No summary / watermark update on retryable.
    assert "summary" not in result.state_updates
    assert "summarized_through_turn_index" not in result.state_updates
    assert any(
        isinstance(e, Tier3SkippedEvent) and e.reason == "retryable"
        for e in result.events
    )


@pytest.mark.asyncio
async def test_tier3_cap_reached_skips_summarization():
    from executor.compaction.defaults import TIER_3_MAX_FIRINGS_PER_TASK

    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)
    state["tier3_firings_count"] = TIER_3_MAX_FIRINGS_PER_TASK

    summarizer = _make_summarizer()
    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    summarizer.assert_not_awaited()
    assert any(
        isinstance(e, Tier3SkippedEvent) and e.reason == "cap_reached"
        for e in result.events
    )


@pytest.mark.asyncio
async def test_fired_event_shape():
    msgs = _build_messages(n_pairs=6)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(summary_text="S"),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    fired = [e for e in result.events if isinstance(e, Tier3FiredEvent)]
    assert len(fired) == 1
    assert fired[0].summarizer_model_id == "test-model"
    assert fired[0].new_summarized_through == find_keep_window_start(msgs)


@pytest.mark.asyncio
async def test_last_super_step_message_count_always_updated():
    """Every hook invocation updates ``last_super_step_message_count``."""
    msgs = _build_messages(n_pairs=3)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(),
        estimate_tokens_fn=_fixed_estimator(100),
    )
    assert result.state_updates["last_super_step_message_count"] == len(msgs)


# ---------------------------------------------------------------------------
# Review follow-ups (2026-04-20) — regression guards for reviewer findings
# ---------------------------------------------------------------------------


def test_graph_threads_summarizer_context_window():
    """``agent_node`` must forward ``summarizer_context_window`` to the hook.

    Without this kwarg, :func:`summarize_slice` skips the recursive-chunking
    path, and any oversized middle trips a non-retryable provider 400 that
    sets ``tier3_fatal_short_circuited`` and permanently dead-letters the
    task. Source-grep because agent_node integration setup is large.
    """
    import re as _re

    graph_path = pathlib.Path(__file__).parent.parent / "executor" / "graph.py"
    src = graph_path.read_text()
    m = _re.search(
        r"await\s+compaction_pre_model_hook\s*\((.*?)\)\s*\n",
        src,
        _re.DOTALL,
    )
    assert m is not None, "compaction_pre_model_hook call not found in graph.py"
    call_args = m.group(1)
    assert "summarizer_context_window" in call_args, (
        "agent_node must forward summarizer_context_window to "
        "compaction_pre_model_hook; otherwise summarize_slice's chunking "
        "path is unreachable and oversized middles dead-letter the task."
    )


@pytest.mark.asyncio
async def test_empty_summary_preserves_watermark():
    """A successful summariser call returning empty/whitespace-only summary
    must NOT advance the watermark.

    Policy-filtered or whitespace-only responses would otherwise silently
    absorb history into a blank summary, making the agent lose context with
    no signal. Surface as a retryable skip instead.
    """
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(summary_text="   "),
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    assert "summary" not in result.state_updates, (
        "empty summary must not overwrite prior summary"
    )
    assert "summarized_through_turn_index" not in result.state_updates, (
        "empty summary must not advance the watermark"
    )
    assert any(
        isinstance(e, Tier3SkippedEvent) and e.reason == "empty_summary"
        for e in result.events
    ), "empty summary must emit a Tier3SkippedEvent(reason='empty_summary')"
    assert result.state_updates.get("tier3_fatal_short_circuited") is not True, (
        "empty summary is retryable, not fatal"
    )


@pytest.mark.asyncio
async def test_summarizer_exception_converted_to_fatal_skip():
    """Unexpected exception from the summariser must be caught and converted
    to a fatal skip — not propagated.

    A typo in ``context_management.summarizer_model`` causes
    ``init_chat_model`` to raise outside the retry loop. If the hook
    propagates, every task for that agent dead-letters with a generic
    non-retryable error and no ``tier3_fatal_short_circuited`` fallback.
    """
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    async def _raising(**_kwargs):
        raise RuntimeError("summariser misconfig: unknown model")

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_raising,
        estimate_tokens_fn=_fixed_estimator(9_500),
    )
    assert result.state_updates.get("tier3_fatal_short_circuited") is True, (
        "unexpected summariser exception must set tier3_fatal_short_circuited"
    )
    assert any(
        isinstance(e, Tier3SkippedEvent) and e.reason == "fatal"
        for e in result.events
    ), "unexpected summariser exception must emit a fatal Tier3SkippedEvent"


@pytest.mark.asyncio
async def test_empty_middle_over_window_shrinks_keep_to_summarise():
    """When ``middle`` is empty and ``est > model_context_window``, the hook
    must shrink the keep window so the summariser has something to reduce.

    Without this rescue a task with exactly ``KEEP_TOOL_USES`` oversized tool
    pairs (or ``>KEEP_TOOL_USES`` with the ``est`` dominated by keep-window
    content) hits ``HardFloorEvent`` with no escape — a silent dead-letter
    despite there being history we could legitimately compact.
    """
    from executor.compaction.defaults import KEEP_TOOL_USES

    # Exactly KEEP_TOOL_USES pairs → find_keep_window_start returns 0, middle
    # is empty without the shrink rescue.
    msgs = _build_messages(n_pairs=KEEP_TOOL_USES)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer(summary_text="SHORT")
    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(15_000),  # well over window
    )

    # Rescue expected: summariser called with non-empty middle (at least the
    # oldest tool pair promoted out of keep_window).
    summarizer.assert_awaited_once()
    call_kwargs = summarizer.await_args.kwargs
    assert len(call_kwargs["slice_messages"]) > 0, (
        "shrink rescue must promote at least one message from keep_window "
        "into middle so the summariser has input"
    )
    # Must not skip as empty_slice.
    assert not any(
        isinstance(e, Tier3SkippedEvent) and e.reason == "empty_slice"
        for e in result.events
    )


@pytest.mark.asyncio
async def test_empty_middle_below_window_stays_empty():
    """When middle is empty and est is BELOW the window, keep behaviour
    unchanged — no need to shrink, no summariser call.
    """
    from executor.compaction.defaults import KEEP_TOOL_USES

    msgs = _build_messages(n_pairs=KEEP_TOOL_USES)
    state = _fresh_state(msgs)

    summarizer = _make_summarizer()
    await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=summarizer,
        estimate_tokens_fn=_fixed_estimator(1_000),  # well under trigger
    )
    summarizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_flush_projection_has_no_tail_system_message():
    """The memory-flush projection must not end with a ``SystemMessage``.

    ``langchain_anthropic._format_messages`` raises
    ``"Received multiple non-consecutive system messages"`` when system
    messages appear after any non-system message, dead-lettering the task.
    """
    msgs = _build_messages(n_pairs=5)
    state = _fresh_state(msgs)

    result = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=_agent_config(memory_enabled=True, pre_flush=True),
        model_context_window=10_000,
        task_context=_task_context(),
        summarizer=_make_summarizer(),
        estimate_tokens_fn=_fixed_estimator(9_500),
        system_prompt="SYSTEM PROMPT",
    )
    # Flush did fire (sanity).
    assert any(isinstance(e, MemoryFlushFiredEvent) for e in result.events)

    projection = result.messages
    first_non_system = next(
        (i for i, m in enumerate(projection) if not isinstance(m, SystemMessage)),
        len(projection),
    )
    for idx, m in enumerate(projection[first_non_system:], start=first_non_system):
        assert not isinstance(m, SystemMessage), (
            f"SystemMessage at position {idx} (after first non-system at "
            f"{first_non_system}) will be rejected by "
            "langchain_anthropic._format_messages."
        )
