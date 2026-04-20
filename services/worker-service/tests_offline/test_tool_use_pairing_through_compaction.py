"""Scenario 2 — tool_use/tool_result pairing preserved across compaction.

Replays the intent of PR #80's "first-firing / second-firing" regression
coverage against a REAL provider, this time using the Option-3
``pre_model_hook`` projection path.

The scenario drives a task with multi-tool-call ``AIMessage`` turns and forces
compaction at a boundary that would naively split a ``tool_use`` from its
matching ``tool_result``. It then:

1. Captures the ``llm_input_messages`` produced by ``pre_model_hook`` on the
   turn after the compaction fired.
2. Runs them through ``LLMConversationShapeValidator`` (which lives at
   ``tests.shape_validator`` — sibling suite, imported for reuse).
3. Asserts validator returns no violations — the hook's projection logic
   preserved tool_use/tool_result pairing across the compaction event.

Gracefully skips when the pre_model_hook module isn't available.
"""

from __future__ import annotations

import pytest


@pytest.mark.offline
def test_tool_use_pairing_preserved_across_compaction(
    offline_provider: str,
    offline_agent_model: str,
    offline_tenant_id: str,
    record_spend,
) -> None:
    pytest.importorskip(
        "executor.compaction.pre_model_hook",
        reason="Task 3 (pre_model_hook) not yet shipped on this branch",
    )
    pytest.skip(
        "Scenario body stub — requires the Track 7 follow-up agent-loop "
        "harness (Tasks 2-5) to be wired in. The shape validator at "
        "tests/shape_validator.py is reused here once the harness lands."
    )
