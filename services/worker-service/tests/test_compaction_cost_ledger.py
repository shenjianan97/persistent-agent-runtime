"""Cost-ledger attribution test (Track 7 AC 9).

AC 9: When Tier 3 fires, exactly one row is written to ``agent_cost_ledger``
tagged ``operation='compaction.tier3'``, attributed to the current task,
agent, and tenant, with non-zero ``tokens_in`` / ``tokens_out``.

This test exercises the ``summarize_slice`` function with a fake cost-ledger
repository and a mocked LLM call. No live DB or LLM credentials are required.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Tier 3 — cost ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)

from executor.compaction.summarizer import (
    SummarizeResult,
    summarize_slice,
)


# ---------------------------------------------------------------------------
# Fake cost-ledger repository (records inserts for inspection)
# ---------------------------------------------------------------------------


@dataclass
class FakeCostLedger:
    """In-memory cost ledger that records every insert call."""

    rows: list[dict[str, Any]] = field(default_factory=list)

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
        self.rows.append({
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "checkpoint_id": checkpoint_id,
            "cost_microdollars": cost_microdollars,
            "operation": operation,
            "model_id": model_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "summarized_through_turn_index_after": summarized_through_turn_index_after,
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slice_messages() -> list:
    """Return a minimal message slice with ≥ 2 messages for summarize_slice."""
    return [
        HumanMessage(content="Perform some research on topic X."),
        AIMessage(
            content="I'll search for information about topic X.",
            tool_calls=[{
                "id": "call_1",
                "name": "web_search",
                "args": {"query": "topic X overview"},
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content="Here are results about topic X: [extensive results]",
            tool_call_id="call_1",
            name="web_search",
        ),
        AIMessage(content="Based on the results, I now know about topic X."),
    ]


def _make_fake_llm_response(
    content: str = "Summary of prior agent work.",
    tokens_in: int = 120,
    tokens_out: int = 40,
) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.response_metadata = {
        "usage": {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
        }
    }
    resp.usage_metadata = None
    return resp


# ---------------------------------------------------------------------------
# Test: successful Tier 3 writes one cost-ledger row tagged compaction.tier3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_writes_cost_ledger_row_tagged_compaction_tier3():
    """Successful Tier 3 must write exactly one row with operation='compaction.tier3'."""
    slice_msgs = _make_slice_messages()
    ledger = FakeCostLedger()
    TOKENS_IN = 250
    TOKENS_OUT = 60
    fake_response = _make_fake_llm_response(tokens_in=TOKENS_IN, tokens_out=TOKENS_OUT)

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=slice_msgs,
            summarizer_model_id="claude-haiku-4-5",
            task_id="task-ac9",
            tenant_id="tenant-ac9",
            agent_id="agent-ac9",
            checkpoint_id="cp-001",
            cost_ledger=ledger,
            summarized_through_turn_index_after=8,
        )

    # Summarization must have succeeded
    assert not result.skipped, f"Expected success but got skipped: {result.skipped_reason}"
    assert result.summary_text is not None

    # Exactly one ledger row must have been written
    assert len(ledger.rows) == 1, (
        f"Expected exactly 1 cost-ledger row, got {len(ledger.rows)}"
    )

    row = ledger.rows[0]
    assert row["operation"] == "compaction.tier3", (
        f"Cost ledger row must be tagged 'compaction.tier3', got {row['operation']!r}"
    )


@pytest.mark.asyncio
async def test_tier3_cost_ledger_row_attribution():
    """Cost-ledger row must carry correct task_id, agent_id, and tenant_id."""
    slice_msgs = _make_slice_messages()
    ledger = FakeCostLedger()
    fake_response = _make_fake_llm_response()

    TASK_ID = "task-attribution-test"
    TENANT_ID = "tenant-attr"
    AGENT_ID = "agent-attr"
    CHECKPOINT_ID = "cp-attr-001"
    WATERMARK_AFTER = 12

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        await summarize_slice(
            slice_messages=slice_msgs,
            summarizer_model_id="test-model",
            task_id=TASK_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            checkpoint_id=CHECKPOINT_ID,
            cost_ledger=ledger,
            summarized_through_turn_index_after=WATERMARK_AFTER,
        )

    assert ledger.rows, "Expected at least one cost-ledger row"
    row = ledger.rows[0]

    assert row["task_id"] == TASK_ID
    assert row["tenant_id"] == TENANT_ID
    assert row["agent_id"] == AGENT_ID
    assert row["checkpoint_id"] == CHECKPOINT_ID
    assert row["summarized_through_turn_index_after"] == WATERMARK_AFTER


@pytest.mark.asyncio
async def test_tier3_cost_ledger_row_has_token_counts():
    """Cost-ledger row must record tokens_in and tokens_out from the LLM response."""
    slice_msgs = _make_slice_messages()
    ledger = FakeCostLedger()
    TOKENS_IN = 350
    TOKENS_OUT = 75
    fake_response = _make_fake_llm_response(tokens_in=TOKENS_IN, tokens_out=TOKENS_OUT)

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=slice_msgs,
            summarizer_model_id="test-model",
            task_id="task-tokens",
            tenant_id="tenant-1",
            agent_id="agent-1",
            checkpoint_id=None,
            cost_ledger=ledger,
        )

    assert result.tokens_in == TOKENS_IN, (
        f"SummarizeResult.tokens_in must match response metadata. Expected {TOKENS_IN}, "
        f"got {result.tokens_in}"
    )
    assert result.tokens_out == TOKENS_OUT
    assert ledger.rows
    row = ledger.rows[0]
    assert row["tokens_in"] == TOKENS_IN
    assert row["tokens_out"] == TOKENS_OUT


@pytest.mark.asyncio
async def test_tier3_cost_ledger_model_id_recorded():
    """Cost-ledger row must record the model_id used for summarization."""
    slice_msgs = _make_slice_messages()
    ledger = FakeCostLedger()
    fake_response = _make_fake_llm_response()
    MODEL_ID = "claude-haiku-4-5"

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=fake_response)
        mock_init.return_value = mock_llm

        await summarize_slice(
            slice_messages=slice_msgs,
            summarizer_model_id=MODEL_ID,
            task_id="task-model",
            tenant_id="tenant-1",
            agent_id="agent-1",
            checkpoint_id=None,
            cost_ledger=ledger,
        )

    assert ledger.rows
    row = ledger.rows[0]
    assert row["model_id"] == MODEL_ID


@pytest.mark.asyncio
async def test_tier3_skipped_no_cost_ledger_row():
    """When Tier 3 is skipped (empty slice), no cost-ledger row must be written."""
    ledger = FakeCostLedger()

    # Single-message slice → empty_slice skip
    result = await summarize_slice(
        slice_messages=[HumanMessage(content="only one message")],
        summarizer_model_id="test-model",
        task_id="task-skip",
        tenant_id="tenant-1",
        agent_id="agent-1",
        checkpoint_id=None,
        cost_ledger=ledger,
    )

    assert result.skipped
    assert result.skipped_reason == "empty_slice"
    assert not ledger.rows, (
        "No cost-ledger row must be written when Tier 3 is skipped"
    )


@pytest.mark.asyncio
async def test_tier3_fatal_error_no_cost_ledger_row():
    """On fatal error, no cost-ledger row must be written."""
    slice_msgs = _make_slice_messages()
    ledger = FakeCostLedger()

    with patch("executor.compaction.summarizer.init_chat_model") as mock_init:
        mock_llm = MagicMock()
        # Raise a non-retryable (fatal) error
        mock_llm.ainvoke = AsyncMock(side_effect=ValueError("invalid model: bad_model"))
        mock_init.return_value = mock_llm

        result = await summarize_slice(
            slice_messages=slice_msgs,
            summarizer_model_id="bad_model",
            task_id="task-fatal",
            tenant_id="tenant-1",
            agent_id="agent-1",
            checkpoint_id=None,
            cost_ledger=ledger,
        )

    assert result.skipped
    assert result.skipped_reason == "fatal"
    assert not ledger.rows, "No cost-ledger row must be written on fatal error"
