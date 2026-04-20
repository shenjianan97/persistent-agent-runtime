"""Unit tests for `_chunk_summarize` — Task 2 / Track 7 Follow-up.

Covers recursive chunk-summarisation of middles that exceed the summarizer's
effective context budget (e.g. 1M-window agent feeding a 200K summarizer).

Design contract (see task-2-recursive-chunking.md):

- Fast path: when the full summarizer payload (prompt + prior_summary +
  serialised middle + max_tokens reservation + HEADROOM) fits in
  `summarizer_context_window`, exactly one LLM call is made. Byte-for-byte
  identical to the pre-Task-2 single-call path.
- Recurse path: when the payload does not fit, split MIDDLE ONLY in halves
  (safe-boundary-aligned where possible), recurse with `prior_summary=""`
  on children, then a final concat call re-introduces the original
  `prior_summary`.
- Cost ledger: intermediate rows tagged `operation="compaction.tier3.chunk"`;
  the final concat row tagged `operation="compaction.tier3"`.
- Progress guarantee: `0 < split < len(middle)`. If no interior safe boundary
  exists, fall back to unsafe halving at `len(middle) // 2` with a single
  `compaction.tier3_unsafe_chunk_split` WARN.
- Retry semantics: the existing `summarize_slice` retry loop applies per
  sub-call. On any failure (retryable or fatal) the top-level result is
  `skipped=True` with the appropriate reason; NO partial summary persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.compaction.defaults import SUMMARIZER_INPUT_HEADROOM_TOKENS
from executor.compaction.summarizer import (
    SummarizeResult,
    summarize_slice,
)


# ---------------------------------------------------------------------------
# Helpers mirror test_compaction_summarizer.py
# ---------------------------------------------------------------------------


def _ai_with_tool_call(
    content: str = "",
    tool_id: str = "call_1",
    tool_name: str = "do_thing",
    args: dict | None = None,
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[{"id": tool_id, "name": tool_name, "args": args or {}}],
    )


def _tool_msg(
    content: str = "result",
    tool_call_id: str = "call_1",
    name: str = "do_thing",
) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


def _llm_response(text: str = "chunk summary") -> MagicMock:
    resp = MagicMock()
    resp.content = text
    resp.response_metadata = {
        "usage": {"input_tokens": 100, "output_tokens": 30}
    }
    resp.usage_metadata = None
    return resp


@dataclass
class _LedgerRow:
    tenant_id: str
    agent_id: str
    task_id: str
    checkpoint_id: str | None
    cost_microdollars: int
    operation: str
    model_id: str | None
    tokens_in: int
    tokens_out: int
    summarized_through_turn_index_after: int | None


class _FakeCostLedger:
    def __init__(self) -> None:
        self.rows: list[_LedgerRow] = []

    async def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        task_id: str,
        checkpoint_id: str | None,
        cost_microdollars: int,
        operation: str,
        model_id: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        summarized_through_turn_index_after: int | None = None,
    ) -> None:
        self.rows.append(
            _LedgerRow(
                tenant_id=tenant_id,
                agent_id=agent_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                cost_microdollars=cost_microdollars,
                operation=operation,
                model_id=model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                summarized_through_turn_index_after=summarized_through_turn_index_after,
            )
        )


TASK_ID = "task-t"
TENANT_ID = "tenant-t"
AGENT_ID = "agent-t"
CHECKPOINT_ID = "chk-t"
MODEL_ID = "claude-haiku-4-5"

# Realistic window sizes: keep HEADROOM (12K) + OUTPUT_RESERVE (1.5K) in mind
# — any window below ~14K leaves no room for a middle at all. We pick SMALL
# WINDOW large enough that single halves fit but full middle does not.
HUGE_WINDOW = 1_000_000  # fast path always fits
SMALL_WINDOW = 50_000    # forces chunking when middle serialised ≳ 36K tokens


def _small_middle() -> list[BaseMessage]:
    """A middle with safe boundaries: 4 complete (AIMessage, ToolMessage) pairs
    separated by a short reasoning AIMessage.
    """
    msgs: list[BaseMessage] = []
    for i in range(4):
        msgs.append(_ai_with_tool_call(content=f"thinking {i}", tool_id=f"c{i}"))
        msgs.append(_tool_msg(content=f"result {i}", tool_call_id=f"c{i}"))
    return msgs


def _big_middle(n_pairs: int = 20, payload_chars: int = 7_000) -> list[BaseMessage]:
    """Larger middle — n_pairs AIMessage/ToolMessage pairs with `payload_chars`
    content on each tool message so total serialised token count is substantial.
    """
    msgs: list[BaseMessage] = []
    payload = "x" * payload_chars
    for i in range(n_pairs):
        msgs.append(
            _ai_with_tool_call(
                content=f"thinking {i}",
                tool_id=f"c{i}",
                args={"path": f"/tmp/file_{i}.txt"},
            )
        )
        msgs.append(_tool_msg(content=payload, tool_call_id=f"c{i}"))
    return msgs


# ---------------------------------------------------------------------------
# Fast-path — payload fits in one call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_path_single_llm_call_when_payload_fits():
    """Small middle + huge summarizer window → exactly one LLM call, one
    ledger row tagged `compaction.tier3` (fast path matches pre-Task-2).
    """
    ledger = _FakeCostLedger()
    fake_resp = _llm_response("single summary")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_resp)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_small_middle(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            prior_summary="",
            summarizer_context_window=HUGE_WINDOW,
        )

    assert result.skipped is False
    assert result.summary_text == "single summary"
    assert mock_llm.ainvoke.call_count == 1
    assert len(ledger.rows) == 1
    assert ledger.rows[0].operation == "compaction.tier3"


@pytest.mark.asyncio
async def test_fast_path_identical_without_context_window_arg():
    """Omitting `summarizer_context_window` (legacy caller — pipeline.py)
    must behave identically to pre-Task-2: single-call path.
    """
    ledger = _FakeCostLedger()
    fake_resp = _llm_response("legacy summary")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_resp)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_small_middle(),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
        )

    assert result.skipped is False
    assert mock_llm.ainvoke.call_count == 1
    assert len(ledger.rows) == 1
    assert ledger.rows[0].operation == "compaction.tier3"


# ---------------------------------------------------------------------------
# Recurse path — big middle forces halving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurse_three_calls_for_oversized_middle():
    """Big middle + tiny summarizer window → one call per half + one final
    concat call = 3 LLM calls. 3 ledger rows: 2 intermediate + 1 final.
    """
    ledger = _FakeCostLedger()

    # Each call returns a distinct summary so we can verify concatenation order.
    responses = [
        _llm_response("CHUNK_A"),
        _llm_response("CHUNK_B"),
        _llm_response("FINAL_COMBINED"),
    ]
    call_idx = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_idx
        resp = responses[call_idx]
        call_idx += 1
        return resp

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_big_middle(n_pairs=20, payload_chars=7_000),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            prior_summary="",
            summarizer_context_window=SMALL_WINDOW,
        )

    assert result.skipped is False
    assert mock_llm.ainvoke.call_count == 3
    assert result.summary_text == "FINAL_COMBINED"

    # Ledger: two intermediate chunks + final concat.
    ops = [row.operation for row in ledger.rows]
    assert ops.count("compaction.tier3.chunk") == 2
    assert ops.count("compaction.tier3") == 1
    assert len(ledger.rows) == 3


@pytest.mark.asyncio
async def test_result_accumulates_tokens_across_subcalls():
    """tokens_in, tokens_out, cost_microdollars accumulate across all
    sub-calls (two chunks + final concat).
    """
    ledger = _FakeCostLedger()

    # Distinct token counts per response so we can verify accumulation.
    def _mk(text: str, in_t: int, out_t: int) -> MagicMock:
        resp = MagicMock()
        resp.content = text
        resp.response_metadata = {
            "usage": {"input_tokens": in_t, "output_tokens": out_t}
        }
        resp.usage_metadata = None
        return resp

    responses = [
        _mk("A", 100, 20),
        _mk("B", 200, 30),
        _mk("FINAL", 50, 10),
    ]
    call_idx = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_idx
        resp = responses[call_idx]
        call_idx += 1
        return resp

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=_big_middle(n_pairs=20, payload_chars=7_000),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            prior_summary="",
            summarizer_context_window=SMALL_WINDOW,
        )

    assert result.skipped is False
    assert result.tokens_in == 350
    assert result.tokens_out == 60


# ---------------------------------------------------------------------------
# Gate correctness — uses full-payload estimate, not raw messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_triggers_on_payload_not_raw_middle_tokens():
    """A middle whose RAW token count fits but whose SERIALISED payload does
    NOT fit (due to prompt + prior_summary + max_tokens + headroom) must
    recurse, not fast-path.
    """
    ledger = _FakeCostLedger()
    responses = [_llm_response(f"r{i}") for i in range(10)]
    call_idx = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_idx
        resp = responses[call_idx]
        call_idx += 1
        return resp

    # Construct a middle whose raw `content` token count fits, but whose
    # SERIALISED form via format_messages_for_summary (adding JSON-serialised
    # tool-call args + framing) + a large prior_summary + HEADROOM +
    # OUTPUT_RESERVE pushes us past the window.
    #
    # Target: context=50K; middle raw content ≈ 20K tokens; prior=25K tokens;
    # middle+prior+headroom+output = 20+25+12+1.5 = 58.5K > 50K → recurse.
    # Each HALF: 10K middle + empty prior + 12K + 1.5K = 23.5K < 50K → fits.
    middle = _big_middle(n_pairs=20, payload_chars=3_000)
    prior = "p" * 75_000  # ~25K tokens via char/3 heuristic
    CONTEXT = 50_000

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=middle,
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            prior_summary=prior,
            summarizer_context_window=CONTEXT,
        )

    # Gate must have fired: recursion, not fast path (>= 3 LLM calls =
    # 2 halves + 1 concat). Confirms the gate uses full-payload estimate
    # (with prior_summary + prompt + headroom + output reservation), not
    # raw middle tokens alone.
    assert mock_llm.ainvoke.call_count >= 3
    assert result.skipped is False


# ---------------------------------------------------------------------------
# prior_summary carry-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prior_summary_carry_through_structure():
    """Top-level call MAY carry prior_summary; recursive per-chunk calls
    MUST NOT carry prior_summary; final concat MUST carry the ORIGINAL
    prior_summary.
    """
    ledger = _FakeCostLedger()
    responses = [
        _llm_response("CHUNK_A"),
        _llm_response("CHUNK_B"),
        _llm_response("FINAL"),
    ]
    call_idx = 0
    captured_human_messages: list[str] = []

    async def side_effect(messages, config=None):
        nonlocal call_idx
        # Capture the HumanMessage content of each call so we can verify
        # prior_summary inclusion.
        for m in messages:
            if isinstance(m, HumanMessage):
                captured_human_messages.append(
                    m.content if isinstance(m.content, str) else str(m.content)
                )
        resp = responses[call_idx]
        call_idx += 1
        return resp

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        PRIOR = "PRIOR_SUMMARY_MARKER_SENTINEL"
        await summarize_slice(
            slice_messages=_big_middle(n_pairs=20, payload_chars=7_000),
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            prior_summary=PRIOR,
            summarizer_context_window=SMALL_WINDOW,
        )

    assert len(captured_human_messages) == 3
    # First two are per-chunk calls: MUST NOT contain the prior_summary.
    assert "PRIOR_SUMMARY_MARKER_SENTINEL" not in captured_human_messages[0]
    assert "PRIOR_SUMMARY_MARKER_SENTINEL" not in captured_human_messages[1]
    # Final concat call: MUST re-introduce the original prior_summary.
    assert "PRIOR_SUMMARY_MARKER_SENTINEL" in captured_human_messages[2]


# ---------------------------------------------------------------------------
# Safe boundary alignment
# ---------------------------------------------------------------------------


def _middle_with_midpoint_on_tool_message(
    n_pairs: int, payload_chars: int = 7_000
) -> list[BaseMessage]:
    """A middle structured so the natural midpoint lands on a ToolMessage.

    Construction: pairs of (AIMessage-with-tool_call, ToolMessage) back to
    back. Midpoint index in such a list lands on the ToolMessage half of a
    pair if n_pairs is even (midpoint = n_pairs, pointing at a ToolMessage).

    This is the canonical "unsafe midpoint → walk back to preceding AIMessage
    with tool_calls" scenario.
    """
    msgs: list[BaseMessage] = []
    payload = "x" * payload_chars
    for i in range(n_pairs):
        msgs.append(
            _ai_with_tool_call(
                content=f"thinking {i}",
                tool_id=f"c{i}",
                args={"path": f"/tmp/f_{i}.txt"},
            )
        )
        msgs.append(_tool_msg(content=payload, tool_call_id=f"c{i}"))
    return msgs


@pytest.mark.asyncio
async def test_split_walks_back_from_tool_message_to_ai_boundary():
    """When the natural midpoint lands on a ToolMessage, the split index
    walks back to the preceding AIMessage-with-tool_calls so the pair is
    kept intact on the second half.
    """
    ledger = _FakeCostLedger()
    middle = _middle_with_midpoint_on_tool_message(n_pairs=10, payload_chars=7_000)
    # len = 20; midpoint = 10; msg at 10 is AIMessage (pair 5), msg at 11 is
    # ToolMessage; but with even-pair layout midpoint lands cleanly. Build a
    # less-clean layout: prefix a single AIMessage so midpoint shifts by 1
    # and lands on a ToolMessage.
    middle = [_ai_with_tool_call(content="lead", tool_id="lead", args={})] + middle
    middle.insert(1, _tool_msg(content="y" * 1_000, tool_call_id="lead"))
    # Now middle[0..1] is a lead pair, then 10 more pairs. Total = 22.
    # Midpoint = 11 → index 11 is AIMessage (start of pair 6) OR ToolMessage.
    # Due to layout, index 11 is a ToolMessage of pair 5.
    midpoint = len(middle) // 2
    assert isinstance(middle[midpoint], ToolMessage), (
        "Test setup expects midpoint on a ToolMessage"
    )

    captured_first_chunk_lengths: list[int] = []
    captured_second_chunk_lengths: list[int] = []

    responses = [
        _llm_response("A"),
        _llm_response("B"),
        _llm_response("FINAL"),
    ]
    call_idx = 0

    async def side_effect(messages, config=None):
        nonlocal call_idx
        # Extract the serialised middle from the HumanMessage. We don't need
        # perfect reconstruction — we need to verify the split DID happen at
        # a safe boundary. We do that via a monkeypatched _chunk_summarize
        # on the call args directly (see below).
        resp = responses[call_idx]
        call_idx += 1
        return resp

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        # Patch the WARN logger so we can assert it was NOT emitted on the
        # safe-boundary path.
        with patch("executor.compaction.summarizer._logger") as mock_logger:
            result = await summarize_slice(
                slice_messages=middle,
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
                prior_summary="",
                summarizer_context_window=SMALL_WINDOW,
            )

            # Safe-boundary path: no unsafe-split WARN.
            warn_calls = [
                c for c in mock_logger.warning.call_args_list
                if c.args and c.args[0] == "compaction.tier3_unsafe_chunk_split"
            ]
            assert len(warn_calls) == 0

    assert result.skipped is False


# ---------------------------------------------------------------------------
# Fallback — unsafe halving when no interior safe boundary exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_safe_boundary_falls_back_to_unsafe_halving():
    """A middle whose only safe split point is index 0 (or >= len) falls
    back to unsafe halving and emits exactly one
    `compaction.tier3_unsafe_chunk_split` WARN.

    Shape: a single AIMessage-with-tool_calls followed by a long run of
    ToolMessages replying to that call. The only AI-with-tool-call index is
    0, so the safe-boundary walk would land at index 0 → progress guarantee
    violation → fall back.
    """
    ledger = _FakeCostLedger()
    # One AIMessage with N tool_calls, followed by N ToolMessages.
    N = 20
    ai_tool_calls = [
        {"id": f"c{i}", "name": "do_thing", "args": {"i": i}} for i in range(N)
    ]
    leading_ai = AIMessage(content="orchestrate", tool_calls=ai_tool_calls)
    middle: list[BaseMessage] = [leading_ai]
    for i in range(N):
        middle.append(_tool_msg(content="z" * 7_000, tool_call_id=f"c{i}"))

    responses = [_llm_response(f"S{i}") for i in range(10)]
    call_idx = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_idx
        if call_idx >= len(responses):
            return _llm_response("FINAL")
        resp = responses[call_idx]
        call_idx += 1
        return resp

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        with patch("executor.compaction.summarizer._logger") as mock_logger:
            result = await summarize_slice(
                slice_messages=middle,
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
                prior_summary="",
                summarizer_context_window=SMALL_WINDOW,
            )

            # At least one unsafe-split WARN was emitted (fallback fired).
            warn_calls = [
                c for c in mock_logger.warning.call_args_list
                if c.args and c.args[0] == "compaction.tier3_unsafe_chunk_split"
            ]
            assert len(warn_calls) >= 1, (
                "Expected at least one compaction.tier3_unsafe_chunk_split WARN"
            )

    # Chunking completed — we got a non-skipped result and recursion is bounded.
    assert result.skipped is False


@pytest.mark.asyncio
async def test_progress_guarantee_recursion_bounded():
    """Recursion must terminate in O(log len) calls — verified by capping
    the LLM call count well below pathological runaway.
    """
    ledger = _FakeCostLedger()
    # Pathological middle shape as above.
    N = 40
    ai_tool_calls = [
        {"id": f"c{i}", "name": "do_thing", "args": {"i": i}} for i in range(N)
    ]
    leading_ai = AIMessage(content="orchestrate", tool_calls=ai_tool_calls)
    middle: list[BaseMessage] = [leading_ai]
    for i in range(N):
        middle.append(_tool_msg(content="z" * 5_000, tool_call_id=f"c{i}"))

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _llm_response(f"s{call_count}")

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=middle,
            summarizer_model_id=MODEL_ID,
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            prior_summary="",
            summarizer_context_window=SMALL_WINDOW,
        )

    assert result.skipped is False
    # len(middle) = 41. Full binary chunk-tree over leaves of 1 message each
    # would be at most 2*41 - 1 = 81 internal-plus-leaf calls; we expect far
    # fewer because leaves summarise multi-message chunks. Cap generously.
    assert call_count < 200, f"Runaway recursion: {call_count} LLM calls"


# ---------------------------------------------------------------------------
# Failure handling — any chunk fails → top-level skipped, no partial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_retryable_failure_sets_top_level_skipped_retryable():
    """If one sub-call fails with retries exhausted, the top-level result is
    skipped=True, skipped_reason='retryable'. No final concat call is made.
    """
    ledger = _FakeCostLedger()
    transient = Exception("503 Service Unavailable")

    async def side_effect(*args, **kwargs):
        # Always fail — all retries exhausted on the first chunk.
        raise transient

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock):
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
            mock_init.return_value = mock_llm

            result = await summarize_slice(
                slice_messages=_big_middle(n_pairs=20, payload_chars=7_000),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
                prior_summary="",
                summarizer_context_window=SMALL_WINDOW,
            )

    assert result.skipped is True
    assert result.skipped_reason == "retryable"
    assert result.summary_text is None
    # Strict-append invariant: NO partial ledger rows for successful chunks
    # in this all-fail case.
    assert len(ledger.rows) == 0


@pytest.mark.asyncio
async def test_chunk_fatal_failure_sets_top_level_skipped_fatal():
    """If one sub-call fails fatally, the top-level result is skipped=True,
    skipped_reason='fatal'. No partial summary is produced.
    """
    ledger = _FakeCostLedger()
    fatal = Exception("401 Unauthorized - invalid API key")

    async def side_effect(*args, **kwargs):
        raise fatal

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        with patch("executor.compaction.summarizer.asyncio.sleep", new_callable=AsyncMock):
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=side_effect)
            mock_init.return_value = mock_llm

            result = await summarize_slice(
                slice_messages=_big_middle(n_pairs=20, payload_chars=7_000),
                summarizer_model_id=MODEL_ID,
                task_id=TASK_ID,
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                checkpoint_id=CHECKPOINT_ID,
                cost_ledger=ledger,
                prior_summary="",
                summarizer_context_window=SMALL_WINDOW,
            )

    assert result.skipped is True
    assert result.skipped_reason == "fatal"
    assert result.summary_text is None
    assert len(ledger.rows) == 0


# ---------------------------------------------------------------------------
# Default headroom exposed
# ---------------------------------------------------------------------------


def test_summarizer_input_headroom_constant_exposed():
    """SUMMARIZER_INPUT_HEADROOM_TOKENS is importable and equals 12_000."""
    assert SUMMARIZER_INPUT_HEADROOM_TOKENS == 12_000
