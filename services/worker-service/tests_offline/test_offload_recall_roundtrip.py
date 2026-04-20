"""Scenario 3 — ingestion offload + recall_tool_result round-trip.

Depends on Tasks 4 (ingestion offload) AND 5 (recall tool + recall-pointer
rewrite) shipping. Both are gated via ``importorskip`` so the scenario cleanly
skips when run against a branch that hasn't landed them yet.

Asserts (per spec acceptance criteria 7):

1. A tool result large enough to trigger ingestion offload lands in S3 with
   only a reference + preview stored inline in ``state["messages"]``.
2. The agent's ``recall_tool_result(tool_call_id)`` call returns the original
   content from S3 and reaches the model on the next turn.
3. **Recall-pointer rewrite** — after a subsequent ``pre_model_hook`` firing
   absorbs the recalled ToolMessage's range into ``summary``, the
   corresponding ``state["messages"]`` entry has its content replaced with a
   pointer string (NOT raw content).
4. A fresh ``recall_tool_result`` call by the agent after the replacement
   still returns the original bytes from S3 (lossless).
"""

from __future__ import annotations

import pytest


@pytest.mark.offline
def test_offload_recall_roundtrip_with_pointer_rewrite(
    offline_provider: str,
    offline_agent_model: str,
    offline_tenant_id: str,
    record_spend,
) -> None:
    pytest.importorskip(
        "executor.compaction.pre_model_hook",
        reason="Task 3 (pre_model_hook) not yet shipped on this branch",
    )
    pytest.importorskip(
        "executor.builtin_tools.recall_tool_result",
        reason="Task 5 (recall_tool_result) not yet shipped on this branch",
    )
    pytest.importorskip(
        "executor.compaction.ingestion",
        reason="Task 4 (ingestion offload) not yet shipped on this branch",
    )
    pytest.skip(
        "Scenario body stub — requires Tasks 4 + 5 (ingestion offload + "
        "recall tool + recall-pointer rewrite) to be wired in. "
        "This is the scenario that exercises the acceptance criterion "
        "for the load-bearing Track 7 follow-up feature."
    )
