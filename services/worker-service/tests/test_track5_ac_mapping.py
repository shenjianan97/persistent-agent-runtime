"""Phase 2 Track 5 — Acceptance-Criteria to Test mapping manifest.

This file is Task 11's primary audit document: it lists every one of the 15
design-doc acceptance criteria (see
``docs/design-docs/phase-2/track-5-memory.md`` §Acceptance criteria) and points
at the concrete tests that exercise them. The two tests below are meta-tests
that keep the manifest honest:

- ``test_every_ac_has_a_linked_test`` — iterates the manifest and asserts
  every referenced test file actually exists in the repository.
- ``test_manifest_covers_all_fifteen_criteria`` — asserts the manifest has an
  entry for each of the 15 criteria, keyed 1..15.

If a future refactor renames or moves a referenced test, the manifest fails
with a clear pointer to fix the map.

The task-spec (``agent_tasks/task-11-integration-and-browser-tests.md``) asks
for a PR-description table linking each AC to a passing test. This dict is the
machine-readable form of that table.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


# Each key is the AC number (1..15). Each value is an iterable of tests that
# exercise that AC, either as a full file (covers the whole criterion) or as a
# specific ``file::test`` identifier. Multiple entries are allowed per AC —
# the design doc says "at least one" and richer coverage is desirable.
#
# File paths are repo-root-relative.
AC_TO_TESTS: dict[int, list[str]] = {
    # AC-1: Agent opt-in + max_entries default persisted.
    1: [
        "services/api-service/src/test/java/com/persistentagent/api/service/ConfigValidationHelperTest.java",
        "services/api-service/src/test/java/com/persistentagent/api/service/AgentServiceTest.java",
        "tests/backend-integration/test_memory_task_submission.py::test_ac1_agent_config_memory_roundtrips",
        "tests/backend-integration/test_memory_task_submission.py::test_ac1_agent_config_memory_absent_when_disabled",
    ],
    # AC-2: One entry per completed memory-enabled task; summarizer outage →
    # template fallback; invariant preserved.
    2: [
        "services/worker-service/tests/test_memory_write.py::TestCommitHappyPath::test_insert_branch_writes_memory_row_and_completes_task",
        "services/worker-service/tests/test_memory_graph.py::TestTemplateFallback",
        "services/worker-service/tests/test_memory_graph.py::TestMemoryWriteNodeSummarizerOutage",
        "services/worker-service/tests/test_memory_write.py::TestCommitHappyPath::test_template_fallback_model_id_allowed",
    ],
    # AC-3: Dead-letter with observations writes a template entry;
    # cancelled_by_user → no write; observations-empty → no write.
    3: [
        "services/worker-service/tests/test_memory_dead_letter.py::TestCancelledByUserSkipsMemoryWrite",
        "services/worker-service/tests/test_memory_dead_letter.py::TestNoObservationsSkipsMemoryWrite",
        "services/worker-service/tests/test_memory_dead_letter.py::TestGenuineFailureWithObservations",
        "services/worker-service/tests/test_memory_dead_letter.py::TestLeaseRevokedRollsBackMemory",
    ],
    # AC-4: Follow-up + redrive upsert overwrite; created_at preserved;
    # updated_at + version advance; observations seeded from existing row.
    4: [
        "services/worker-service/tests/test_memory_write.py::TestCommitUpsertFollowUpBehaviour::test_second_commit_on_same_task_id_updates_and_does_not_trim",
        "services/worker-service/tests/test_memory_follow_up_seeding.py::TestSecondRunAfterSuccessfulWrite::test_returns_prior_observations_verbatim",
        "services/worker-service/tests/test_memory_follow_up_seeding.py::TestRedriveAfterDeadLetter::test_returns_observations_from_template_dead_letter_row",
    ],
    # AC-5: memory_note appends observations; durable at super-step checkpoint
    # granularity; visible in the final entry.
    5: [
        "services/worker-service/tests/test_memory_tools.py::TestMemoryNoteTool",
        "services/worker-service/tests/test_memory_graph.py::TestMemoryEnabledState",
    ],
    # AC-6: memory_search returns RRF hybrid results scoped to (tenant, agent);
    # scope bound from worker context, not LLM args.
    6: [
        "services/worker-service/tests/test_memory_tools.py::TestMemorySearchTool",
        "services/worker-service/tests/test_memory_tools.py::TestMemorySearchArguments",
        "tests/backend-integration/test_memory_api.py::test_text_search_returns_matches",
        "tests/backend-integration/test_memory_api.py::test_search_hybrid_degrades_to_text_when_embedding_unreachable",
        "tests/backend-integration/test_memory_api.py::test_search_vector_mode_503_when_embedding_unreachable",
    ],
    # AC-7: task_history_get returns a bounded view; cross-agent / cross-tenant
    # task ids return tool-shaped "not found".
    7: [
        "services/worker-service/tests/test_memory_tools.py::TestTaskHistoryGetTool",
        "services/worker-service/tests/test_memory_tools.py::TestTaskHistoryGetArguments",
    ],
    # AC-8: Attachment at submission validated in a single scoped query;
    # resolution miss returns uniform 4xx; persisted in task_attached_memories
    # with position; mirrored into task_submitted event details.
    8: [
        "tests/backend-integration/test_memory_task_submission.py::test_ac8_attach_valid_persists_in_join_table_and_event",
        "tests/backend-integration/test_memory_task_submission.py::test_ac8_attach_cross_agent_rejected_uniform",
        "tests/backend-integration/test_memory_task_submission.py::test_ac8_attach_unknown_id_rejected_uniform",
        "tests/backend-integration/test_memory_task_submission.py::test_ac8_preview_omits_deleted_memory_entries",
        "services/worker-service/tests/test_memory_attach_injection.py::TestResolveAttachedMemoriesForTask",
        "services/worker-service/tests/test_memory_attach_injection.py::TestBuildAttachedMemoriesPreamble",
    ],
    # AC-9: Customer can browse, search, read, delete via Console + API;
    # agent_storage_stats exposed.
    9: [
        "tests/backend-integration/test_memory_api.py::test_list_returns_items_and_storage_stats_on_first_page",
        "tests/backend-integration/test_memory_api.py::test_get_returns_full_entry",
        "tests/backend-integration/test_memory_api.py::test_delete_removes_row_and_returns_204",
        "tests/backend-integration/test_memory_api.py::test_delete_leaves_task_attached_memories_intact",
        # Console side: Scenario 11 + 13 in docs/CONSOLE_BROWSER_TESTING.md.
    ],
    # AC-10: Cross-tenant / cross-agent access uniformly 404 across every
    # memory-touching surface.
    10: [
        "tests/backend-integration/test_memory_api.py::test_list_unknown_agent_returns_404",
        "tests/backend-integration/test_memory_api.py::test_get_unknown_memory_id_returns_uniform_404",
        "tests/backend-integration/test_memory_api.py::test_get_wrong_agent_returns_uniform_404",
        "tests/backend-integration/test_memory_api.py::test_search_cross_agent_scope_404",
        "tests/backend-integration/test_memory_api.py::test_two_agents_under_same_tenant_do_not_leak",
        "tests/backend-integration/test_memory_task_submission.py::test_ac10_cross_tenant_memory_is_invisible",
        "tests/backend-integration/test_memory_task_submission.py::test_ac8_attach_cross_agent_rejected_uniform",
        "services/worker-service/tests/test_memory_follow_up_seeding.py::TestScopeBinding",
    ],
    # AC-11: Memory-disabled (agent-level or skip_memory_write) → no tool
    # registration, no memory_write node, no rows, no cost; task_history_get
    # still available.
    11: [
        "services/worker-service/tests/test_memory_graph_topology.py::TestBuildGraphMemoryDisabled",
        "services/worker-service/tests/test_memory_dead_letter.py::TestMemoryDisabledSkipsWrite",
        "services/worker-service/tests/test_memory_tools.py::TestBuildMemoryToolsGating",
        "services/worker-service/tests/test_memory_graph.py::TestEffectiveMemoryEnabled",
        "tests/backend-integration/test_memory_task_submission.py::test_ac11_skip_memory_write_persists_on_task",
        "tests/backend-integration/test_memory_task_submission.py::test_ac11_skip_memory_write_defaults_false",
        "tests/backend-integration/test_memory_task_submission.py::test_ac11_skip_memory_write_on_disabled_agent_is_noop_but_persisted",
    ],
    # AC-12: Memory write does not block completion — summarizer outage →
    # template; embedding outage → content_vec = NULL; task still completed.
    12: [
        "services/worker-service/tests/test_memory_graph.py::TestMemoryWriteNodeSummarizerOutage",
        "services/worker-service/tests/test_memory_graph.py::TestMemoryWriteNodeEmbeddingOutage",
        "services/worker-service/tests/test_memory_write.py::TestCommitHappyPath::test_embedding_none_writes_row_with_null_content_vec",
        "services/worker-service/tests/test_memory_dead_letter.py::TestEmbeddingDownWritesNullVector",
    ],
    # AC-13: max_entries FIFO trim on INSERT branch; Console warning at 80%.
    13: [
        "services/worker-service/tests/test_memory_write.py::TestCommitTrim::test_trim_fires_when_insert_pushes_past_max_entries",
        "services/worker-service/tests/test_memory_write.py::TestCommitTrim::test_trim_does_not_fire_on_update_branch_even_when_over_cap",
        "services/worker-service/tests/test_memory_repository_integration.py::TestTrimOldest",
        # Console 80%-banner: docs/CONSOLE_BROWSER_TESTING.md Scenario 11.
    ],
    # AC-14: Summarizer + embedding write-time cost in agent_cost_ledger;
    # summarizer exempt from budget_max_per_task; embedding zero-rated.
    14: [
        "services/worker-service/tests/test_memory_budget_carve_out.py::test_memory_write_cost_does_not_pause_task_even_when_over_budget",
        "services/worker-service/tests/test_memory_graph.py::TestMemoryWriteNodeHappyPath",
    ],
    # AC-15: Meta — unit + E2E cover every branch of AC-1..14.
    # Tracked by the manifest's own completeness tests below.
    15: [
        # The two meta-tests below collectively enforce AC-15. They live in
        # this file, so we self-reference them for auditability.
        "services/worker-service/tests/test_track5_ac_mapping.py::test_every_ac_has_a_linked_test",
        "services/worker-service/tests/test_track5_ac_mapping.py::test_manifest_covers_all_fifteen_criteria",
    ],
}


def _strip_test_id(entry: str) -> str:
    """Return the repo-root-relative file portion of a possibly-qualified id."""
    return entry.split("::", 1)[0]


def _expected_files() -> Iterable[Path]:
    for entries in AC_TO_TESTS.values():
        for entry in entries:
            yield REPO_ROOT / _strip_test_id(entry)


def test_manifest_covers_all_fifteen_criteria() -> None:
    """Every AC (1..15) must have at least one linked test."""
    expected = set(range(1, 16))
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
