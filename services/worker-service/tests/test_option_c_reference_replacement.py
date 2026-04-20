"""Unit tests for Phase 2 Track 7 Follow-up Task 5 — Option C.

Option C is the ONE sanctioned mutation to ``state["messages"]`` under the
replace-and-rehydrate architecture: on a compaction firing that advances
``summarized_through`` past a recalled ``ToolMessage``'s position, the hook
replaces its ``content`` with a short reference string pointing at the
original ``tool_call_id``. The original content stays in S3 — a fresh
``recall_tool_result`` call still returns it byte-for-byte.

Covers (see task-5 spec §6):

* Replacement fires only on recalled ToolMessages inside
  ``[previous_summarized_through, new_summarized_through)``.
* Replacement content matches the canonical placeholder string.
* ``additional_kwargs["content_offloaded"] = True`` is set;
  ``original_tool_call_id`` is preserved.
* Recovery path — after replacement, a direct ``recall_tool_result`` call
  still returns the original content (the artefact store is the source of
  truth; Option C is lossless).
* Projection rule — recalled ToolMessages outside the keep window are
  dropped from ``middle`` in the hook's output.
* Idempotence — running Option C twice on the same range is a no-op (no
  churn through the reducer).
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

from executor.compaction.pre_model_hook import (
    CompactionPassResult,
    _is_recalled_tool_message,
    _reference_placeholder,
    compaction_pre_model_hook,
    option_c_reference_replacement,
)
from executor.compaction.tool_result_store import InMemoryToolResultStore


TENANT = "tenant-c"
TASK = "task-c"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recalled_tool_message(
    *,
    msg_id: str,
    tool_call_id: str,
    original_tool_call_id: str,
    content: str,
) -> ToolMessage:
    m = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name="recall_tool_result",
        additional_kwargs={
            "recalled": True,
            "original_tool_call_id": original_tool_call_id,
        },
        id=msg_id,
    )
    return m


# ---------------------------------------------------------------------------
# option_c_reference_replacement — direct unit tests
# ---------------------------------------------------------------------------


def test_replacement_fires_for_recalled_message_in_newly_summarized_range():
    raw = [
        HumanMessage(content="kickoff", id="h1"),
        _recalled_tool_message(
            msg_id="t1",
            tool_call_id="tooluse_new_recall_1",
            original_tool_call_id="tooluse_orig_xyz",
            content="FULL RECALLED PAYLOAD",
        ),
        AIMessage(content="ok", id="a1"),
    ]

    replacements = option_c_reference_replacement(
        raw,
        previous_summarized_through=0,
        new_summarized_through=3,
    )

    assert len(replacements) == 1
    r = replacements[0]
    assert isinstance(r, ToolMessage)
    assert r.id == "t1"
    assert r.tool_call_id == "tooluse_new_recall_1"
    assert r.content == _reference_placeholder("tooluse_orig_xyz")
    assert r.additional_kwargs["content_offloaded"] is True
    assert r.additional_kwargs["original_tool_call_id"] == "tooluse_orig_xyz"
    assert r.additional_kwargs["recalled"] is True


def test_replacement_does_not_fire_for_message_outside_range():
    raw = [
        HumanMessage(content="kickoff", id="h1"),
        _recalled_tool_message(
            msg_id="t1",
            tool_call_id="tooluse_new_1",
            original_tool_call_id="tooluse_orig_1",
            content="old recalled",
        ),
        _recalled_tool_message(
            msg_id="t2",
            tool_call_id="tooluse_new_2",
            original_tool_call_id="tooluse_orig_2",
            content="recent recalled",
        ),
    ]

    # Only the first recalled message is inside [0, 2)
    replacements = option_c_reference_replacement(
        raw,
        previous_summarized_through=0,
        new_summarized_through=2,
    )
    assert len(replacements) == 1
    assert replacements[0].id == "t1"


def test_replacement_idempotent_when_already_offloaded():
    # Simulate a second compaction pass that would cover an already-
    # reference-replaced recalled message. Option C must no-op so the
    # reducer doesn't churn the id.
    already_replaced = ToolMessage(
        content=_reference_placeholder("tooluse_orig_1"),
        tool_call_id="tooluse_new_1",
        name="recall_tool_result",
        additional_kwargs={
            "recalled": True,
            "original_tool_call_id": "tooluse_orig_1",
            "content_offloaded": True,
        },
        id="t1",
    )
    raw = [HumanMessage(content="seed", id="h1"), already_replaced]
    replacements = option_c_reference_replacement(
        raw,
        previous_summarized_through=0,
        new_summarized_through=2,
    )
    assert replacements == []


def test_replacement_ignores_non_recalled_tool_messages():
    raw = [
        AIMessage(content="", id="a1", tool_calls=[{"id": "c1", "name": "x", "args": {}}]),
        ToolMessage(content="normal result", tool_call_id="c1", name="x", id="t1"),
    ]
    replacements = option_c_reference_replacement(
        raw,
        previous_summarized_through=0,
        new_summarized_through=2,
    )
    assert replacements == []


def test_replacement_no_op_when_range_empty():
    raw = [
        _recalled_tool_message(
            msg_id="t1",
            tool_call_id="c1",
            original_tool_call_id="orig1",
            content="payload",
        )
    ]
    assert option_c_reference_replacement(
        raw, previous_summarized_through=5, new_summarized_through=3
    ) == []
    assert option_c_reference_replacement(
        raw, previous_summarized_through=3, new_summarized_through=3
    ) == []


# ---------------------------------------------------------------------------
# Recovery: S3 still holds the original content after Option C fires
# ---------------------------------------------------------------------------


async def test_option_c_is_lossless_s3_still_returns_original_content():
    """The artefact store is the source of truth: even after Option C
    replaces the journal entry's ``content``, a fresh ``recall_tool_result``
    call still returns the original bytes from S3."""
    from executor.builtin_tools.recall_tool_result import _resolve_and_fetch
    from executor.builtin_tools.recall_tool_result import _RecallContext

    store = InMemoryToolResultStore()
    original_content = "the full original payload " * 1000
    original_tool_call_id = "tooluse_orig_lossless"
    await store.put(
        tenant_id=TENANT,
        task_id=TASK,
        tool_call_id=original_tool_call_id,
        content=original_content,
    )

    # Apply Option C — the journal entry's content is replaced with a
    # placeholder, but the S3 artefact is untouched.
    raw = [
        _recalled_tool_message(
            msg_id="t1",
            tool_call_id="tooluse_recall_1",
            original_tool_call_id=original_tool_call_id,
            content=original_content,
        )
    ]
    replacements = option_c_reference_replacement(
        raw, previous_summarized_through=0, new_summarized_through=1
    )
    assert len(replacements) == 1
    assert replacements[0].content != original_content  # placeholder, not payload

    # A fresh recall with the ORIGINAL tool_call_id still returns the bytes.
    ctx = _RecallContext(tenant_id=TENANT, task_id=TASK, store=store)
    out = await _resolve_and_fetch(
        ctx=ctx, tool_call_id=original_tool_call_id, arg_key=None
    )
    assert out == original_content


# ---------------------------------------------------------------------------
# Integration with the hook — projection drops recalled outside keep window
# ---------------------------------------------------------------------------


def _estimate_small(msgs: list[BaseMessage]) -> int:
    """Deterministic token estimator — 10 tokens per message."""
    return 10 * len(msgs)


async def test_projection_drops_recalled_messages_outside_keep_window():
    """Recalled ``ToolMessage`` entries sitting in ``middle`` (i.e. OUTSIDE
    the keep window) are excluded from the projection the hook hands to the
    LLM. Inside the keep window they're rendered verbatim."""
    # Build a journal long enough that find_keep_window_start keeps only
    # the last KEEP_TOOL_USES=3 ToolMessages. We'll put one recalled
    # message inside the keep window and one outside.
    msgs: list[BaseMessage] = [
        HumanMessage(content="hi"),
    ]
    # Four tool-use cycles — the first one is the "old recalled" (outside
    # keep window); the last three fill the keep window.
    for i in range(4):
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[{"id": f"c{i}", "name": "x", "args": {}}],
            )
        )
        if i == 0:
            # Old recalled — outside the keep window
            msgs.append(
                _recalled_tool_message(
                    msg_id=f"tm{i}",
                    tool_call_id=f"c{i}",
                    original_tool_call_id="tooluse_orig_old",
                    content="OLD RECALLED PAYLOAD",
                )
            )
        elif i == 3:
            # Inside keep window — recalled, rendered verbatim
            msgs.append(
                _recalled_tool_message(
                    msg_id=f"tm{i}",
                    tool_call_id=f"c{i}",
                    original_tool_call_id="tooluse_orig_recent",
                    content="RECENT RECALLED PAYLOAD",
                )
            )
        else:
            msgs.append(ToolMessage(content="ok", tool_call_id=f"c{i}"))

    state: dict[str, Any] = {
        "messages": msgs,
        "summary": "",
        "summarized_through_turn_index": 0,
    }
    agent_config: dict[str, Any] = {}

    result: CompactionPassResult = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=100_000,  # well above trigger → no summarisation
        task_context={"tenant_id": TENANT, "agent_id": "a", "task_id": TASK},
        summarizer=_unused_summarizer,
        estimate_tokens_fn=_estimate_small,
    )

    # The old recalled message (outside keep window, in "middle") is DROPPED.
    contents = [m.content for m in result.messages if isinstance(m, ToolMessage)]
    assert "OLD RECALLED PAYLOAD" not in contents
    # The recent recalled message (inside keep window) is RENDERED VERBATIM.
    assert "RECENT RECALLED PAYLOAD" in contents


async def _unused_summarizer(**kwargs: Any) -> Any:
    # Must not be called when est_tokens < trigger_tokens
    raise AssertionError("summariser should not fire below trigger")


# ---------------------------------------------------------------------------
# End-to-end: hook writes Option C replacements into state_updates["messages"]
# ---------------------------------------------------------------------------


class _FakeSummarizeResult:
    def __init__(self, text: str) -> None:
        self.summary_text = text
        self.summarizer_model_id = "claude-haiku-4-5"
        self.tokens_in = 100
        self.tokens_out = 50
        self.skipped = False
        self.skipped_reason = None


async def test_hook_emits_option_c_replacement_in_state_updates_messages():
    """Full hook flow: when the trigger fires and the watermark advances
    past a recalled ToolMessage, ``state_updates["messages"]`` carries the
    replacement so LangGraph's ``add_messages`` reducer swaps it in place."""

    async def _summariser(**kwargs: Any) -> Any:
        return _FakeSummarizeResult("fresh summary of everything")

    msgs: list[BaseMessage] = [
        HumanMessage(content="go"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c0", "name": "recall_tool_result", "args": {}}],
        ),
        _recalled_tool_message(
            msg_id="tm0",
            tool_call_id="c0",
            original_tool_call_id="tooluse_orig_to_absorb",
            content="THIS CONTENT WILL BE SUMMARISED",
        ),
    ]
    # Fill up the keep window with three MORE tool uses so the recalled
    # message at index 2 falls OUTSIDE the keep window.
    for i in range(1, 4):
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[{"id": f"c{i}", "name": "x", "args": {}}],
            )
        )
        msgs.append(ToolMessage(content=f"r{i}", tool_call_id=f"c{i}"))

    state: dict[str, Any] = {
        "messages": msgs,
        "summary": "",
        "summarized_through_turn_index": 0,
    }
    agent_config: dict[str, Any] = {}

    # Force the trigger: the projection has ~8 messages after the recalled
    # drop rule (2 middle + 6 keep-window). With 10 tokens/message = 80,
    # pick a model window small enough that 0.85 * window < 80.
    result: CompactionPassResult = await compaction_pre_model_hook(
        raw_messages=msgs,
        state=state,
        agent_config=agent_config,
        model_context_window=40,
        task_context={"tenant_id": TENANT, "agent_id": "a", "task_id": TASK},
        summarizer=_summariser,
        estimate_tokens_fn=_estimate_small,
    )

    # Summarisation fired — new summary + watermark advanced.
    assert "summary" in result.state_updates
    assert result.state_updates["summary"] == "fresh summary of everything"
    new_through = result.state_updates["summarized_through_turn_index"]
    assert new_through >= 2  # past our recalled message at index 2

    # Option C replacement is in the state update.
    assert "messages" in result.state_updates
    replacements = result.state_updates["messages"]
    assert len(replacements) == 1
    r = replacements[0]
    assert r.id == "tm0"
    assert "tooluse_orig_to_absorb" in r.content
    assert r.additional_kwargs["content_offloaded"] is True


def test_is_recalled_tool_message_recogniser():
    assert _is_recalled_tool_message(
        _recalled_tool_message(
            msg_id="x",
            tool_call_id="c",
            original_tool_call_id="o",
            content="c",
        )
    )
    assert not _is_recalled_tool_message(
        ToolMessage(content="r", tool_call_id="c")
    )
    assert not _is_recalled_tool_message(SystemMessage(content="s"))
