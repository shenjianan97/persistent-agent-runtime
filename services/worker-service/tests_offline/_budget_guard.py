"""Per-run budget kill-switch for the offline evaluation suite.

Cross-run / daily-cumulative spend tracking is explicitly out of scope for v1
(see ``docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-6-offline-llm-eval-suite.md``
§3 "Budget kill-switch"). Each pytest invocation gets its own budget; matrix
jobs in the workflow each have their own budget too. Worst-case nightly spend is
``3 × PER_RUN_OFFLINE_BUDGET_MICRODOLLARS ≈ $3 × 30 = $90 / month``.

This module owns the in-process accumulator used by ``conftest.py``'s
``pytest_runtest_teardown`` hook. Scenarios call :func:`record_scenario_spend`
with the microdollars they observed in the ephemeral ledger after running. The
guard in :func:`check_budget_and_maybe_skip` is what causes subsequent scenarios
to skip cleanly (``pytest.skip(...)``) rather than silently overrunning.

Scenarios that do not exercise real providers (e.g. ``test_multi_provider_smoke``
running only a single provider per matrix slice) may record zero spend; the
accumulator still walks forward.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

# Per-run hard cap (~$1 USD). Matches the task spec exactly.
PER_RUN_OFFLINE_BUDGET_MICRODOLLARS = 1_000_000

# Env-var escape hatch for local runs and manual workflow_dispatch. CI's
# scheduled/tagged triggers should NOT set this; leaving it unset preserves the
# $1 cap.
_ENV_OVERRIDE = "OFFLINE_LLM_EVAL_BUDGET_MICRODOLLARS"


@dataclass
class _BudgetState:
    spent_microdollars: int = 0
    cap_microdollars: int = PER_RUN_OFFLINE_BUDGET_MICRODOLLARS
    # Names of scenarios that were skipped because the cap was exceeded —
    # used for the workflow annotation emitted at session teardown.
    skipped_nodeids: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _resolve_cap() -> int:
    raw = os.environ.get(_ENV_OVERRIDE)
    if not raw:
        return PER_RUN_OFFLINE_BUDGET_MICRODOLLARS
    try:
        parsed = int(raw)
    except ValueError:
        return PER_RUN_OFFLINE_BUDGET_MICRODOLLARS
    # Negative or zero overrides are treated as "use default" — a zero cap would
    # skip every scenario, which is never the intent.
    if parsed <= 0:
        return PER_RUN_OFFLINE_BUDGET_MICRODOLLARS
    return parsed


_STATE = _BudgetState(cap_microdollars=_resolve_cap())


def reset_for_tests() -> None:
    """Test-only helper to reset the module-level accumulator."""
    global _STATE
    _STATE = _BudgetState(cap_microdollars=_resolve_cap())


def record_scenario_spend(microdollars: int) -> None:
    """Add a completed scenario's spend to the per-run accumulator.

    Scenarios call this after their own run (either from a fixture teardown or
    directly at the end of the test body) with the delta they observed in the
    ephemeral cost ledger. Negative values are clamped to 0.
    """
    delta = max(0, int(microdollars))
    with _STATE.lock:
        _STATE.spent_microdollars += delta


def current_spend_microdollars() -> int:
    with _STATE.lock:
        return _STATE.spent_microdollars


def cap_microdollars() -> int:
    with _STATE.lock:
        return _STATE.cap_microdollars


def is_over_cap() -> bool:
    with _STATE.lock:
        return _STATE.spent_microdollars >= _STATE.cap_microdollars


def note_skipped(nodeid: str) -> None:
    with _STATE.lock:
        _STATE.skipped_nodeids.append(nodeid)


def skipped_nodeids() -> list[str]:
    with _STATE.lock:
        return list(_STATE.skipped_nodeids)
