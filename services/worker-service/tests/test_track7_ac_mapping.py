"""Phase 2 Track 7 — Acceptance-Criteria to Test mapping manifest.

This file is Task 12's primary audit document: it lists every one of the 14
design-doc acceptance criteria (see
``docs/design-docs/phase-2/track-7-context-window-management.md``
§Acceptance criteria) and points at the concrete tests that exercise them.

Two meta-tests enforce the manifest:

- ``test_every_ac_has_a_linked_test`` — asserts every referenced test file
  actually exists in the repository.
- ``test_manifest_covers_all_fourteen_criteria`` — asserts the manifest has an
  entry for each of the 14 criteria, keyed 1..14.

If a future refactor renames or moves a referenced test, the manifest fails
with a clear pointer to fix the map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


# Each key is the AC number (1..14). Each value is an iterable of tests that
# exercise that AC, either as a full file (covers the whole criterion) or as a
# specific ``file::test`` identifier. Multiple entries are allowed per AC.
#
# File paths are repo-root-relative.
AC_TO_TESTS: dict[int, list[str]] = {
    # AC-1 — All agents serve LLM calls with raw history below Tier 1 threshold
    # with masked/truncated history above it.
    1: [
        "services/worker-service/tests/test_graph_compaction_integration.py",
        "services/worker-service/tests/test_compaction_pipeline.py::test_tier1_fires_when_over_threshold",
    ],
    # AC-2 — Per-tool-result cap applied before ToolMessage enters state; structured
    # log compaction.per_result_capped emitted.
    2: [
        "services/worker-service/tests/test_graph_tool_cap_integration.py",
        "services/worker-service/tests/test_compaction_caps.py",
        "services/worker-service/tests/test_compaction_observability.py::test_per_result_capped_event_fires_above_cap",
    ],
    # AC-3 — Tier 3 fires only when Tier 1+1.5 together cannot bring input below
    # TIER_3_TRIGGER_FRACTION.
    3: [
        "services/worker-service/tests/test_compaction_pipeline.py::test_tier3_fires_only_when_tier1_insufficient",
        "services/worker-service/tests/test_compaction_pipeline.py::test_tier3_not_fired_when_tier1_sufficient",
    ],
    # AC-4 — Watermark fields on graph state only advance; a unit test that feeds
    # back a regressing watermark confirms the reducer ignores it.
    4: [
        "services/worker-service/tests/test_compaction_state_reducers.py::TestMaxReducer::test_max_reducer_stale_does_not_regress",
        "services/worker-service/tests/test_compaction_state_reducers.py::TestCheckpointBackwardCompat::test_stale_watermark_does_not_regress_in_graph",
    ],
    # AC-5 — Cache-stability invariant: running the same compaction pipeline on
    # the same state twice produces byte-identical output.
    5: [
        "services/worker-service/tests/test_compaction_cache_stability.py::test_cache_stability_tier1_only",
        "services/worker-service/tests/test_compaction_cache_stability.py::test_cache_stability_tier3_fires",
        "services/worker-service/tests/test_compaction_cache_stability.py::test_cache_stability_no_op_path",
        "services/worker-service/tests/test_compaction_pipeline.py::test_cache_stability_identical_output_on_second_call",
    ],
    # AC-6 — exclude_tools entries are never masked. Given a task with memory_note
    # results scattered through history, after Tier 1 runs every memory_note
    # ToolMessage retains its original content.
    6: [
        "services/worker-service/tests/test_compaction_exclude_tools.py::test_memory_note_never_cleared_by_tier1",
        "services/worker-service/tests/test_compaction_exclude_tools.py::test_pipeline_exclude_tools_never_cleared_by_tier1",
        "services/worker-service/tests/test_compaction_exclude_tools.py::test_agent_exclude_tools_union_with_platform_list",
    ],
    # AC-7 — Pre-Tier-3 memory flush fires at most once per task. Fires for agents
    # with memory.enabled=true AND pre_tier3_memory_flush=true. Does not fire on
    # heartbeat / recovery turns. Survives redrive (one-shot flag restored).
    7: [
        "services/worker-service/tests/test_compaction_pre_tier3_flush.py::test_flush_fires_when_all_conditions_true_and_over_tier3",
        "services/worker-service/tests/test_compaction_pre_tier3_flush.py::test_flush_fires_only_once_across_two_calls",
        "services/worker-service/tests/test_compaction_pre_tier3_flush.py::test_flush_does_not_fire_on_heartbeat_turn",
        "services/worker-service/tests/test_compaction_pre_tier3_flush_redrive.py::test_redrive_from_post_flush_checkpoint_does_not_refire",
        "services/worker-service/tests/test_compaction_pre_tier3_flush_redrive.py::test_flush_fires_exactly_once_across_redrive_cycle",
    ],
    # AC-8 — summary_marker is append-only. A second Tier 3 firing within the same
    # task appends a new summary rather than rewriting the existing one.
    8: [
        "services/worker-service/tests/test_compaction_summary_marker_append.py::test_second_tier3_appends_to_marker",
        "services/worker-service/tests/test_compaction_summary_marker_append.py::test_strict_append_reducer_rejects_non_append",
        "services/worker-service/tests/test_compaction_summary_marker_append.py::test_strict_append_reducer_emits_log_on_rejection",
        "services/worker-service/tests/test_compaction_state_reducers.py::TestSummaryMarkerStrictAppendReducer::test_non_append_rejected_returns_a",
        "services/worker-service/tests/test_compaction_state_reducers.py::TestSummaryMarkerStrictAppendReducer::test_non_append_logs_structured_event",
    ],
    # AC-9 — Tier 3 cost lands in agent_cost_ledger tagged compaction.tier3,
    # attributed to the current task and checkpoint.
    9: [
        "services/worker-service/tests/test_compaction_cost_ledger.py::test_tier3_writes_cost_ledger_row_tagged_compaction_tier3",
        "services/worker-service/tests/test_compaction_cost_ledger.py::test_tier3_cost_ledger_row_attribution",
        "services/worker-service/tests/test_compaction_cost_ledger.py::test_tier3_cost_ledger_row_has_token_counts",
        "services/worker-service/tests/test_compaction_summarizer.py",
    ],
    # AC-10 — Budget carve-out: tasks with budget_max_per_task close to Tier 3 cost
    # do not pause mid-summarization.
    10: [
        "services/worker-service/tests/test_compaction_budget_carve_out.py::TestCompactionTier3BudgetCarveOut::test_compaction_tier3_in_graph_py_source",
        "services/worker-service/tests/test_graph_compaction_integration.py::TestBudgetCarveOut::test_compaction_tier3_in_carve_out",
    ],
    # AC-11 — Dead-letter with reason context_exceeded_irrecoverable transitions
    # the task cleanly.
    11: [
        "services/worker-service/tests/test_dead_letter_check_constraints_integration.py",
        "services/worker-service/tests/test_compaction_pipeline.py::test_hard_floor_event_emitted_when_still_over_limit",
    ],
    # AC-12 — POST/PUT /v1/agents validates context_management fields.
    # summarizer_model pointing at inactive/wrong-provider model returns 400.
    12: [
        "tests/backend-integration/test_context_management_validation.py",
    ],
    # AC-13 — Memory-disabled agents never fire the pre-Tier-3 flush, even if
    # pre_tier3_memory_flush=true in their config.
    13: [
        "services/worker-service/tests/test_compaction_memory_disabled_no_flush.py::test_memory_disabled_flush_never_fires",
        "services/worker-service/tests/test_compaction_memory_disabled_no_flush.py::test_memory_disabled_no_flush_system_message_in_compacted",
        "services/worker-service/tests/test_compaction_pre_tier3_flush.py::test_flush_does_not_fire_when_memory_disabled",
    ],
    # AC-14 — Langfuse trace of a task that exercised all three tiers shows one
    # compaction.tier3 span per firing, one compaction.inline span per call that
    # fires tier 1/1.5, and per-result cap annotations on affected tool spans.
    # Automated: structured-log events assert correct shapes (this task).
    # Manual: orchestrator Playwright Scenario 16 confirms Langfuse UI.
    14: [
        "services/worker-service/tests/test_compaction_observability.py::test_per_result_capped_event_fires_above_cap",
        "services/worker-service/tests/test_compaction_observability.py::test_tier1_applied_event_emitted_when_threshold_crossed",
        "services/worker-service/tests/test_compaction_observability.py::test_tier3_fired_event_emitted_on_success",
        "services/worker-service/tests/test_compaction_observability.py::test_memory_flush_fired_event_emitted_when_flush_fires",
        "services/worker-service/tests/test_compaction_observability.py::test_hard_floor_event_emitted_when_still_over_context",
    ],
}


def _strip_test_id(entry: str) -> str:
    """Return the repo-root-relative file portion of a possibly-qualified id."""
    return entry.split("::", 1)[0]


def _expected_files() -> Iterable[Path]:
    for entries in AC_TO_TESTS.values():
        for entry in entries:
            yield REPO_ROOT / _strip_test_id(entry)


def test_manifest_covers_all_fourteen_criteria() -> None:
    """Every AC (1..14) must have at least one linked test."""
    expected = set(range(1, 15))
    assert set(AC_TO_TESTS.keys()) == expected, (
        f"Missing AC keys: {expected - set(AC_TO_TESTS.keys())}, "
        f"unexpected: {set(AC_TO_TESTS.keys()) - expected}"
    )
    for ac, tests in AC_TO_TESTS.items():
        assert tests, f"AC-{ac} has no linked tests"


def test_every_ac_has_a_linked_test() -> None:
    """Every file referenced in the manifest must exist on disk.

    This catches renames / moves that would otherwise leave the manifest out
    of sync with the suite silently.
    """
    missing: list[str] = []
    for path in _expected_files():
        if not path.is_file():
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "The following manifest-referenced files are missing from the tree:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


@pytest.mark.parametrize("ac", sorted(AC_TO_TESTS.keys()))
def test_ac_has_nonempty_mapping(ac: int) -> None:
    """Per-AC parametrised coverage check — surfaces the gap per row."""
    assert AC_TO_TESTS[ac], f"AC-{ac} has no linked tests"
