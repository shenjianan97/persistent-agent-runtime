"""Conftest for the real-provider offline evaluation suite.

Responsibilities:

1. Register the ``offline`` pytest marker (also declared in
   ``pyproject.toml``'s ``tool.pytest.ini_options.markers`` — the duplicate
   here is defensive so running ``pytest tests_offline/`` standalone still
   recognises the marker if someone invokes without the project config).

2. Enforce the per-run budget kill-switch. After each scenario completes, the
   ``pytest_runtest_teardown`` hook checks cumulative spend against the cap
   from :mod:`_budget_guard`. If over, every subsequent scenario is skipped
   via ``pytest.skip("per-run budget exceeded")``.

3. Provide a tiny fixture (:func:`record_spend`) that scenarios can use to add
   their observed cost to the accumulator without importing the guard module
   directly.

Intentionally NOT provided here:

* A fresh Postgres container — scenarios run against the ephemeral DB that the
  workflow's service container provides (``E2E_DB_*`` env vars mirror
  ``make e2e-test``). Locally, scenarios must either be skipped or run against
  ``par-e2e-postgres`` on port 55433.
* Real credentials — secrets come from the workflow's ``secrets`` context.
  Local runs require the developer to export the same env vars themselves.

The budget guard is process-local. Matrix jobs for different providers each
have their own cap; see ``_budget_guard`` module doc for the rationale.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

from tests_offline import _budget_guard


def pytest_configure(config: pytest.Config) -> None:  # pragma: no cover - plugin hook
    """Register the ``offline`` marker defensively.

    ``pyproject.toml`` also registers it, but when the suite is invoked from a
    different working directory or with an overridden config, re-registering
    here avoids spurious ``PytestUnknownMarkWarning`` noise.
    """
    config.addinivalue_line(
        "markers",
        "offline: real-provider offline evaluation suite "
        "(scheduled only; never on per-commit CI)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:  # pragma: no cover - plugin hook
    """Auto-apply ``@pytest.mark.offline`` to every test under ``tests_offline/``.

    Scenarios already mark themselves explicitly (acceptance criterion #1 calls
    out ``grep -r "@pytest.mark.offline" tests_offline/``), but applying the
    mark at collection time is belt-and-suspenders: even a scenario author who
    forgets the decorator still gets the marker, which matters for the
    ``-m offline`` selector used in the workflow invocation.
    """
    for item in items:
        item.add_marker(pytest.mark.offline)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:  # pragma: no cover - plugin hook
    """After each scenario, check the budget and skip remaining if over cap.

    We don't know the next item's ID deterministically here (pytest may reorder
    or a user may have filtered), so the guard is *sticky*: once
    :func:`_budget_guard.is_over_cap` trips, every subsequent scenario's setup
    raises ``pytest.skip``. See :func:`_pre_setup_budget_check` below for the
    actual skip trigger — this hook only records state.
    """
    # Scenario-specific teardown has already run at this point, so any
    # ``record_spend`` fixture or direct call has already landed.
    if _budget_guard.is_over_cap() and nextitem is not None:
        _budget_guard.note_skipped(nextitem.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:  # pragma: no cover - plugin hook
    """Skip at setup if the per-run budget has already been exceeded.

    ``tryfirst=True`` lets us short-circuit before any scenario-level fixture
    runs and potentially incurs more API cost.
    """
    if _budget_guard.is_over_cap():
        pytest.skip("per-run budget exceeded")


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # pragma: no cover - plugin hook
    """Emit a final line summarising budget usage and skipped scenarios.

    The GitHub Actions workflow greps this section for ``OFFLINE_BUDGET_*`` to
    convert into a workflow annotation.
    """
    spent = _budget_guard.current_spend_microdollars()
    cap = _budget_guard.cap_microdollars()
    skipped = _budget_guard.skipped_nodeids()
    terminalreporter.write_sep("=", "offline eval budget summary")
    terminalreporter.write_line(
        f"OFFLINE_BUDGET_SPENT_MICRODOLLARS={spent}"
    )
    terminalreporter.write_line(f"OFFLINE_BUDGET_CAP_MICRODOLLARS={cap}")
    if skipped:
        terminalreporter.write_line(
            f"OFFLINE_BUDGET_SKIPPED_SCENARIOS={len(skipped)}"
        )
        for nodeid in skipped:
            terminalreporter.write_line(f"  skipped: {nodeid}")


@pytest.fixture
def record_spend() -> Iterator[callable]:
    """Scenario-facing helper to add microdollars to the per-run accumulator.

    Example::

        def test_whatever(record_spend):
            # ... run scenario, observe cost via ephemeral ledger ...
            record_spend(12_345)  # ≈ $0.012
    """
    yield _budget_guard.record_scenario_spend


@pytest.fixture(scope="session")
def offline_tenant_id() -> str:
    """Reserved tenant_id so cost-ledger queries can filter offline spend."""
    return os.environ.get("OFFLINE_LLM_EVAL_TENANT_ID", "offline-llm-eval")


@pytest.fixture(scope="session")
def offline_provider() -> str:
    """Provider slug for the current matrix cell (``bedrock`` / ``anthropic`` / ``openai``).

    Defaults to ``anthropic`` for local runs where the matrix variable isn't set.
    Scenarios that target a specific provider should compare against this and
    ``pytest.skip`` if they don't match.
    """
    return os.environ.get("OFFLINE_LLM_EVAL_PROVIDER", "anthropic")


@pytest.fixture(scope="session")
def offline_agent_model() -> str:
    """Default-cheap agent model for scenarios; override via env var."""
    return os.environ.get(
        "OFFLINE_LLM_EVAL_AGENT_MODEL",
        "claude-haiku-4-5",
    )
