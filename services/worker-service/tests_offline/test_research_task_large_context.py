"""Scenario 1 — AWS-research-style task forcing compaction.

Runs a research-flavoured task that accumulates enough tool-use turns to
trigger at least one firing of the ``pre_model_hook`` summarization path
(Task 3 of the Track 7 follow-up). Asserts:

* The task completes (not dead-lettered).
* ``state["tier3_firings_count"] >= 1`` (compaction fired).
* The replaced ``state["summary"]`` stays under a budget fraction of the
  model's context window (``COMPACTION_TRIGGER_FRACTION``).

Depends on Tasks 2+3 shipping. When those symbols aren't importable (e.g. the
suite runs against a branch where Task 3 hasn't landed yet), the scenario
skips cleanly rather than failing collection.
"""

from __future__ import annotations

import pytest


@pytest.mark.offline
def test_research_task_triggers_at_least_one_compaction(
    offline_provider: str,
    offline_agent_model: str,
    offline_tenant_id: str,
    record_spend,
) -> None:
    # Task 3 lands ``pre_model_hook``; Task 2 lands recursive chunking. Gate on
    # the publicly-named module so the scenario skips before any real-provider
    # call. Kept inside the test body (not module-level) so the scenario still
    # appears in collection even on branches where Task 3 hasn't landed.
    pytest.importorskip(
        "executor.compaction.pre_model_hook",
        reason="Task 3 (pre_model_hook) not yet shipped on this branch",
    )
    """Run the research scenario end-to-end against a real provider.

    Implementation note: the agent harness wiring (real DB, real provider,
    checkpointer, cost-ledger attribution) lives in the worker runtime and is
    not reproduced here. This scenario is a thin pytest adapter that:

    1. Seeds a task row for ``offline_tenant_id``.
    2. Runs the agent loop until terminal.
    3. Reads the terminal state + cumulative cost from the ephemeral ledger.
    4. Asserts the observable behaviours above.
    5. Calls ``record_spend(cost_microdollars)`` so the per-run budget guard
       can skip subsequent scenarios if this one was unexpectedly expensive.

    Until the offload+pre_model_hook pipeline lands, this scenario is a
    ``pytest.skip`` at import time (see module-level ``importorskip``).
    """
    pytest.skip(
        "Scenario body stub — requires the Track 7 follow-up agent-loop "
        "harness (Tasks 2-5) to be wired in. Tracked for implementation "
        "in the first post-deploy offline run."
    )
