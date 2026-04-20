"""Scenario 4 — memory flush path and ``context_exceeded_irrecoverable``.

Two assertions under one scenario because both exercise the edge of the
compaction pipeline's control flow:

1. **Memory flush once-per-task** — memory-enabled agent crosses the
   compaction threshold. Assert ``state["memory_flush_fired_this_task"]``
   is ``True`` after the firing AND that the flush didn't fire twice
   (the monotone-``or`` reducer prevents it, but we assert observable
   state too).

2. **Dead-letter path on pathological input** — a task whose minimum keep
   window (system + last human + last tool-use pair) itself exceeds the
   model's context budget MUST dead-letter with reason
   ``context_exceeded_irrecoverable`` rather than loop the summarizer.
"""

from __future__ import annotations

import pytest


@pytest.mark.offline
def test_memory_flush_fires_exactly_once(
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
        "harness and a memory-enabled agent fixture to be wired in."
    )


@pytest.mark.offline
def test_pathological_input_dead_letters_with_context_exceeded(
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
        "harness and a dead-letter-reason inspection fixture."
    )
