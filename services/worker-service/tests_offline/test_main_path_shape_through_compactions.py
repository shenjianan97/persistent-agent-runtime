"""Scenario 6 — MAIN agent-LLM-path shape validation across compactions.

This is the scenario that addresses the real provider-shape regression class
PR #80 caught in production. The summarizer sub-path is *already* covered by
the unit-test shape-property suite; the MAIN agent path — what the agent LLM
sees on successive turns after a ``pre_model_hook`` firing — is where the
Bedrock/Anthropic/OpenAI adapters diverge and where PR #80's three bugs
lived.

The scenario:

1. Drives a task with enough tool-use turns to force the ``pre_model_hook``
   to fire compaction at least two times (ideally three, to cover
   first-firing / second-firing / repeated-firing).
2. On each turn that crosses a compaction boundary, captures
   ``llm_input_messages`` returned by ``pre_model_hook``.
3. Runs each captured projection through ``LLMConversationShapeValidator``
   (reused from ``tests/shape_validator.py``).
4. Asserts the validator reports no violations for ALL captured projections.

Acceptance criteria 6 calls for shape-validator cleanliness at firings 1, 2,
and 3 specifically.
"""

from __future__ import annotations

import pytest


@pytest.mark.offline
def test_main_path_shape_clean_across_three_compactions(
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
        "harness plus a ``pre_model_hook`` tap that records each firing's "
        "projection for post-run validation."
    )
