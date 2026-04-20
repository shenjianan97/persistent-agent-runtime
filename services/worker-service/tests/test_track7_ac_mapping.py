"""Phase 2 Track 7 Follow-up (Task 3) — Acceptance-Criteria → Test mapping.

The original Track 7 design (three-tier in-place transforms) has been
superseded by the Track 7 Follow-up's replace-and-rehydrate
``pre_model_hook`` pipeline (Task 3). This manifest points at the tests
that exercise the 14 acceptance criteria in
``docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/
task-3-pre-model-hook-architecture.md``.

Two meta-tests enforce the manifest:

- ``test_every_ac_has_a_linked_test`` — every referenced file exists.
- ``test_manifest_covers_all_fourteen_criteria`` — keys 1..14 are present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


# Each key is the AC number (1..14) from the Task 3 spec. Entries list tests
# that exercise that AC, either as full files or ``file::test`` identifiers.
AC_TO_TESTS: dict[int, list[str]] = {
    # AC-1 — compact_for_llm no longer exists; pre_model_hook is the entry point.
    1: [
        "services/worker-service/tests/test_pre_model_hook.py::test_compact_for_llm_symbol_gone",
        "services/worker-service/tests/test_pre_model_hook.py::test_graph_wires_pre_model_hook",
    ],
    # AC-2 — RuntimeState schema shape (legacy fields gone; new fields present).
    2: [
        "services/worker-service/tests/test_runtime_state_schema.py::TestRuntimeStateSchemaShape::test_track7_followup_fields_present",
        "services/worker-service/tests/test_runtime_state_schema.py::TestRuntimeStateSchemaShape::test_legacy_fields_removed",
    ],
    # AC-3 — three-region projection order.
    3: [
        "services/worker-service/tests/test_pre_model_hook.py::test_projection_region_order",
        "services/worker-service/tests/test_pre_model_hook.py::test_projection_omits_summary_when_empty",
    ],
    # AC-4 — Summariser receives RAW middle (never stubbed).
    4: [
        "services/worker-service/tests/test_pre_model_hook.py::test_summarizer_receives_raw_middle",
    ],
    # AC-5 — Main LLM never sees stubs.
    5: [
        "services/worker-service/tests/test_pre_model_hook.py::test_main_llm_sees_no_stubs",
    ],
    # AC-6 — Post-summarisation state (replace, not append).
    6: [
        "services/worker-service/tests/test_pre_model_hook.py::test_post_firing_state_replace_semantics",
        "services/worker-service/tests/test_pre_model_hook.py::test_journal_not_mutated_on_firing",
    ],
    # AC-7 — Summarisation trigger fraction (0.85 × context window).
    7: [
        "services/worker-service/tests/test_pre_model_hook.py::test_trigger_at_or_above_compaction_fraction",
        "services/worker-service/tests/test_pre_model_hook.py::test_no_summarizer_below_threshold",
    ],
    # AC-8 — Keep-window orphan alignment.
    8: [
        "services/worker-service/tests/test_pre_model_hook.py::test_keep_window_orphan_alignment",
        "services/worker-service/tests/test_pre_model_hook.py::test_keep_window_with_few_tools_returns_zero",
    ],
    # AC-9 — Pre-summarisation memory flush preserved.
    9: [
        "services/worker-service/tests/test_pre_model_hook.py::test_memory_flush_fires_when_all_conditions_hold",
        "services/worker-service/tests/test_pre_model_hook.py::test_memory_flush_does_not_fire_twice",
        "services/worker-service/tests/test_pre_model_hook.py::test_memory_flush_skipped_when_memory_disabled",
    ],
    # AC-10 — Hypothesis property test.
    10: [
        "services/worker-service/tests/test_compaction_shape_property.py",
    ],
    # AC-11 — Chunking integration (forwarded via summarizer_context_window).
    11: [
        "services/worker-service/tests/test_pre_model_hook.py::test_summarizer_context_window_forwarded",
    ],
    # AC-12 — Dead-letter / hard-floor path.
    12: [
        "services/worker-service/tests/test_pre_model_hook.py::test_hard_floor_event_emitted_when_over_window",
    ],
    # AC-13 — Firing-rate regression budget (asserted as a bound in Task 6's
    # offline suite; unit-level this manifest pins the invariant that one
    # firing per invocation is the cap at the hook level).
    13: [
        "services/worker-service/tests/test_pre_model_hook.py::test_single_firing_per_invocation",
    ],
    # AC-14 — Append-only invariant on state["messages"].
    14: [
        "services/worker-service/tests/test_pre_model_hook.py::test_journal_append_only_across_turns",
    ],
}


def _strip_test_id(entry: str) -> str:
    return entry.split("::", 1)[0]


def _expected_files() -> Iterable[Path]:
    for entries in AC_TO_TESTS.values():
        for entry in entries:
            yield REPO_ROOT / _strip_test_id(entry)


def test_manifest_covers_all_fourteen_criteria() -> None:
    expected = set(range(1, 15))
    assert set(AC_TO_TESTS.keys()) == expected, (
        f"Missing AC keys: {expected - set(AC_TO_TESTS.keys())}, "
        f"unexpected: {set(AC_TO_TESTS.keys()) - expected}"
    )
    for ac, tests in AC_TO_TESTS.items():
        assert tests, f"AC-{ac} has no linked tests"


def test_every_ac_has_a_linked_test() -> None:
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
    assert AC_TO_TESTS[ac], f"AC-{ac} has no linked tests"
