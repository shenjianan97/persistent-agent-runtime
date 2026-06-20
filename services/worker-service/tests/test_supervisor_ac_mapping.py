"""Supervisor Topology (S11) — Behavior-to-Test mapping manifest.

This is S11's primary audit document: it lists the nine observable behaviors the
Supervisor topology must exhibit (task spec
``agent_tasks/task-s11-supervisor-integration-tests.md`` §"Observable behaviors to
cover") and points each at the concrete test(s) that exercise it. Mirrors the
Track-5 precedent (``test_track5_ac_mapping.py``): the meta-tests below keep the
manifest honest — a rename/move of any referenced test fails the manifest with a
clear pointer.

Several behaviors are ALREADY covered by earlier Stream-S tasks; for those the
manifest POINTS at the existing test rather than duplicating it (the orchestrator
note: reconcile, don't clobber):

* #1 fan-out determinism      — S6 (structural) + S11 (event-level)
* #2 partial failure → proceed — S6 (node) + S11 (composed graph)
* #3 zero return → fail        — S6 (node) + S11 (composed graph)
* #4 citation binding + verify + immutability — S7
* #5 crash resume-forward      — S6 (sub-wiring) + S11 (composed) + S3 (real Postgres)
* #6 operator redrive recomputes — S11 (composed graph)
* #7 caps enforced             — S6 (node) + S11 (composed graph)
* #8 budget rolls into parent  — S8 (real Postgres) + S11 (live execute_task resume)
* #9 research-preset create + tree render — Playwright Scenario 21 (orchestrator runs)

For #9 (Playwright) the meta-test asserts the scenario block exists in
``CONSOLE_BROWSER_TESTING.md`` (grep for the heading) — it does NOT execute the
browser (orchestrator-owned, AGENTS.md §Browser verification is the orchestrator's
job).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]

# Each key is the behavior number (1..9). Each value is an iterable of tests that
# exercise that behavior, either as a full file (covers the whole behavior) or a
# ``file::test`` identifier. File paths are repo-root-relative. Multiple entries
# per behavior are allowed — richer coverage is desirable.
BEHAVIOR_TO_TESTS: dict[int, list[str]] = {
    # #1 Fan-out determinism — N subtasks → N Send branches in one super-step; N
    # subagent_started events, distinct subtask ids. Structural, not LLM-emergent.
    1: [
        "services/worker-service/tests/test_supervisor_fanout.py::test_send_fanout_dispatches_n_subagents",
        "services/worker-service/tests/test_supervisor_fanout.py::test_within_iteration_collision_both_results_survive",
        "services/worker-service/tests/test_supervisor_fanout_events.py::test_fanout_emits_n_distinct_subagent_started_under_one_iteration",
    ],
    # #2 Partial failure → proceed — one sub-agent fails, findings present for the
    # rest, Writer runs, subagent_failed{reason} emitted, run does NOT error.
    2: [
        "services/worker-service/tests/test_supervisor_fanout.py::test_partial_failure_with_one_success_proceeds",
        "services/worker-service/tests/test_supervisor_partial_failure.py::test_partial_failure_one_subagent_fails_writer_still_runs",
    ],
    # #3 Zero return → fail — ALL sub-agents fail → terminal failure (not a silent
    # empty report). In-graph terminal failure, never a dead-lettered sub-task.
    3: [
        "services/worker-service/tests/test_supervisor_fanout.py::test_zero_returns_no_progress_fails",
        "services/worker-service/tests/test_supervisor_partial_failure.py::test_zero_return_all_fail_reaches_terminal_failure",
    ],
    # #4 Citation binding — Writer cites resolvable finding_ids; unresolvable →
    # render-error flag (no fabricated source); verify flags an unsupported quote
    # WITHOUT mutating supporting_quote (immutability, §A0.4).
    4: [
        "services/worker-service/tests/test_supervisor_citations.py::test_resolve_miss_returns_render_error_flag_never_fabricates",
        "services/worker-service/tests/test_supervisor_citations.py::test_verify_flags_unsupported_passes_supported",
        "services/worker-service/tests/test_supervisor_citations.py::test_verify_does_not_invent_source_for_unknown_id",
        "services/worker-service/tests/test_supervisor_citations.py::test_reduce_findings_never_mutates_supporting_quote",
    ],
    # #5 Crash resume-forward — completed siblings restored from subagent_results,
    # NOT recomputed; only unfinished branches re-run. Sub-wiring (S6) + composed
    # graph (S11) + real Postgres at the checkpointer level (S3).
    5: [
        "services/worker-service/tests/test_supervisor_fanout.py::test_crash_resume_forward_restores_completed_siblings",
        "services/worker-service/tests/test_supervisor_redrive.py::test_crash_resume_forward_reuses_completed_siblings_composed",
        "tests/backend-integration/test_subagent_fanout_durability.py::test_subagent_per_inner_turn_resume_does_not_recompute",
        "tests/backend-integration/test_subagent_fanout_durability.py::test_subagent_fanout_durability_on_postgres",
    ],
    # #6 Operator redrive recomputes — rollback_last_checkpoint re-runs the fan-out
    # super-step (mock fires again → new tokens). Contrast with #5: resume-forward
    # reuses, redrive recomputes.
    6: [
        "services/worker-service/tests/test_supervisor_redrive.py::test_operator_redrive_recomputes_fanout_superstep",
    ],
    # #7 Caps enforced — max_fanout_per_iteration clamps (cap-reason event);
    # max_iterations stops the loop (final stop-reason supervisor_iteration event).
    7: [
        "services/worker-service/tests/test_supervisor_fanout.py::test_clamp_to_max_fanout_emits_cap_event",
        "services/worker-service/tests/test_supervisor_fanout.py::test_max_iterations_forces_stop",
        "services/worker-service/tests/test_supervisor_caps.py::test_max_fanout_clamps_dispatched_send_branches",
        "services/worker-service/tests/test_supervisor_caps.py::test_max_iterations_stops_loop_and_reaches_writer",
    ],
    # #8 Budget rolls into parent — sub-agent cost attributed to the PARENT task
    # under the existing operation at the super-step checkpoint_id; no sub_agent_id
    # column, no per-sub-agent rows (§A0.1); over-budget PAUSES (Track 3), never
    # silently fails. Plus the live execute_task-level paused-task resume (S11).
    8: [
        "tests/backend-integration/test_supervisor_fanout_budget.py::test_supervisor_fanout_budget_records_nonzero_parent_cost",
        "tests/backend-integration/test_supervisor_fanout_budget.py::test_supervisor_budget_pause_fires_at_fanout_boundary_billing_each_sibling_once",
        "tests/backend-integration/test_supervisor_live_resume.py::test_paused_supervisor_task_resumes_through_execute_task",
    ],
    # #9 Research-preset create + tree render — Playwright scenario (orchestrator
    # runs). The REST E2E covers the marker projection + one-task-row server-side.
    9: [
        "tests/backend-integration/test_supervisor_research_e2e.py::test_research_preset_run_projects_tree_markers_one_task_row",
        # Playwright Scenario 21 in docs/CONSOLE_BROWSER_TESTING.md (asserted by
        # test_behavior_9_playwright_scenario_exists below).
    ],
}

# Behavior #9's browser coverage is a scenario heading in the Console testing doc,
# not a pytest file. The grep target is the canonical Scenario 21 heading S10 added.
CONSOLE_DOC = REPO_ROOT / "docs" / "CONSOLE_BROWSER_TESTING.md"
SCENARIO_21_HEADING = "### Scenario 21: Deep Research Preset + Sub-agent Activity Tree"


def _strip_test_id(entry: str) -> str:
    return entry.split("::", 1)[0]


def _expected_files() -> Iterable[Path]:
    for entries in BEHAVIOR_TO_TESTS.values():
        for entry in entries:
            yield REPO_ROOT / _strip_test_id(entry)


def test_manifest_covers_all_nine_behaviors() -> None:
    """Every behavior (1..9) must have at least one linked test/scenario."""
    expected = set(range(1, 10))
    assert set(BEHAVIOR_TO_TESTS.keys()) == expected, (
        f"Missing behavior keys: {expected - set(BEHAVIOR_TO_TESTS.keys())}, "
        f"unexpected: {set(BEHAVIOR_TO_TESTS.keys()) - expected}"
    )
    for behavior, tests in BEHAVIOR_TO_TESTS.items():
        assert tests, f"Behavior #{behavior} has no linked tests"


def test_every_behavior_has_an_existing_test_file() -> None:
    """Every file referenced in the manifest must exist on disk — catches a
    rename/move that would otherwise leave the manifest silently stale."""
    missing: list[str] = []
    for path in _expected_files():
        if not path.is_file():
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "Manifest-referenced files missing from the tree:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


@pytest.mark.parametrize("behavior", sorted(BEHAVIOR_TO_TESTS.keys()))
def test_behavior_has_nonempty_mapping(behavior: int) -> None:
    """Per-behavior coverage check — surfaces the gap per row."""
    assert BEHAVIOR_TO_TESTS[behavior], f"Behavior #{behavior} has no linked tests"


def test_behavior_9_playwright_scenario_exists() -> None:
    """Behavior #9's browser coverage is Scenario 21 in CONSOLE_BROWSER_TESTING.md.
    Assert the scenario heading exists (the orchestrator EXECUTES it — this only
    confirms the scenario text is present, never runs the browser)."""
    assert CONSOLE_DOC.is_file(), f"missing {CONSOLE_DOC}"
    text = CONSOLE_DOC.read_text(encoding="utf-8")
    assert SCENARIO_21_HEADING in text, (
        f"Scenario 21 heading not found in {CONSOLE_DOC.name}; behavior #9 "
        "(research-preset create + sub-agent tree) is unverified in the browser doc"
    )
