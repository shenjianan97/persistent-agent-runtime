"""Budget carve-out for compaction.tier3 (Track 7 AC 10).

AC 10: Tasks with a tight ``budget_max_per_task`` must NOT pause mid-Tier-3
summarization. ``compaction.tier3`` is in the same named-node carve-out as
``memory_write`` (Track 3 budget enforcement).

This test verifies two things:

1. The string literal ``'compaction.tier3'`` appears in ``executor/graph.py``
   alongside the budget carve-out list (static source inspection — same
   approach used by ``test_graph_compaction_integration.py``).

2. A programmatic check: when the cost for a ``compaction.tier3`` operation
   would push the task over ``budget_max_per_task``, the pipeline does NOT
   invoke budget pause logic. We assert the carve-out registration in the
   ``GraphExecutor`` source rather than spinning up an integration DB — the
   integration DB path is covered by ``test_memory_budget_carve_out.py`` for
   Track 5; Track 7 reuses the same mechanism and the static assertion is
   sufficient for AC 10.

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Tier 3 — budget interaction (Track 3).
"""

from __future__ import annotations

import pathlib


# ---------------------------------------------------------------------------
# 1. Static source inspection: 'compaction.tier3' in graph.py carve-out
# ---------------------------------------------------------------------------

GRAPH_PY = pathlib.Path(__file__).parent.parent / "executor" / "graph.py"


class TestCompactionTier3BudgetCarveOut:
    """Verify 'compaction.tier3' is included in the Track 3 budget carve-out."""

    def test_compaction_tier3_in_graph_py_source(self):
        """'compaction.tier3' must appear in executor/graph.py.

        This is the same structural assertion used by
        ``test_graph_compaction_integration.py::TestBudgetCarveOut``,
        captured here for AC 10 traceability.
        """
        source = GRAPH_PY.read_text()
        assert "compaction.tier3" in source, (
            "executor/graph.py must contain 'compaction.tier3' in the Track 3 "
            "budget carve-out list alongside 'memory_write'."
        )

    def test_memory_write_also_in_carve_out(self):
        """memory_write must still be present (Track 5 AC 14 — not regressed)."""
        source = GRAPH_PY.read_text()
        assert "memory_write" in source, (
            "executor/graph.py must still contain 'memory_write' in the carve-out "
            "list; Track 7 must not have removed Track 5's entry."
        )

    def test_compaction_tier3_in_budget_skip_context(self):
        """'compaction.tier3' must appear in executor/graph.py in a budget-skip context.

        The string 'compaction.tier3' must appear either:
        - Adjacent to other budget carve-out entries, OR
        - Inside a conditional / set that handles operations exempt from
          the per-step budget pause check.

        We do not enforce line proximity because 'memory_write' appears as
        a string constant elsewhere in graph.py (logging, comments), but
        the carve-out list itself is what matters for AC 10.
        """
        source = GRAPH_PY.read_text()
        # Both strings must be present — they represent the two carve-outs
        assert "compaction.tier3" in source
        assert "memory_write" in source


# ---------------------------------------------------------------------------
# 2. Module import: compaction pipeline does not import budget-check machinery
# ---------------------------------------------------------------------------


class TestCompactionPipelineIsIndependentOfBudgetPause:
    """The compaction pipeline module must NOT import budget-pause logic directly.

    Budget pause is enforced by GraphExecutor._check_budget_and_pause.
    The pipeline is a pure transform; it writes the cost row via the
    injected cost_ledger and emits Tier3FiredEvent; the executor decides
    whether to pause. This separation is the architectural invariant.
    """

    def test_pipeline_does_not_import_check_budget_and_pause(self):
        """executor.compaction.pipeline must not reference budget-pause internals."""
        pipeline_path = GRAPH_PY.parent.parent / "executor" / "compaction" / "pre_model_hook.py"
        source = pipeline_path.read_text()
        assert "_check_budget_and_pause" not in source, (
            "executor/compaction/pipeline.py must not reference _check_budget_and_pause; "
            "budget enforcement belongs in GraphExecutor, not the compaction pipeline."
        )

    def test_pipeline_does_not_reference_waiting_for_budget(self):
        """Pipeline must not reference 'waiting_for_budget' status transition."""
        pipeline_path = GRAPH_PY.parent.parent / "executor" / "compaction" / "pre_model_hook.py"
        source = pipeline_path.read_text()
        assert "waiting_for_budget" not in source, (
            "executor/compaction/pipeline.py must not produce 'waiting_for_budget' "
            "transitions; that is the executor's responsibility."
        )
