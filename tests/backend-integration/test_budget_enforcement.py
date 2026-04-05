"""Integration tests for Track 3 budget enforcement.

Since the mock LLM does not produce real token usage metadata (response_metadata
is empty on MagicMock AIMessages), these tests use DB manipulation to simulate
cost accrual and trigger budget pauses. This mirrors the HITL test pattern of
using direct DB updates to put tasks into specific states.

Tests:
- Per-task budget pause with correct pause_reason and pause_details
- Hourly budget pause with auto-recovery info
- Budget precedence (per-task wins when both exceeded)
- Hourly budget blocks new claims
- task_paused event details schema
"""

import asyncio
import json
import uuid

import pytest

from helpers.api_client import ApiError
from helpers.mock_llm import simple_response


TENANT_ID = "default"


@pytest.mark.asyncio
class TestBudgetEnforcement:

    async def _simulate_per_task_budget_pause(self, e2e, agent_id, task_id,
                                               budget_max_per_task, observed_cost):
        """Simulate a per-task budget pause by directly manipulating the DB.

        This mirrors how the executor would pause a task when cumulative cost
        exceeds budget_max_per_task at a checkpoint boundary.
        """
        pause_details = json.dumps({
            "budget_max_per_task": budget_max_per_task,
            "observed_task_cost_microdollars": observed_cost,
            "recovery_mode": "manual_resume_after_budget_increase",
        })

        # Insert cost into the ledger
        await e2e.db.execute(
            """INSERT INTO agent_cost_ledger
                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
               VALUES ($1, $2, $3::uuid, $4, $5)""",
            TENANT_ID, agent_id, task_id, str(uuid.uuid4()), observed_cost,
        )

        # Transition task to paused
        await e2e.db.execute(
            """UPDATE tasks
               SET status = 'paused',
                   pause_reason = 'budget_per_task',
                   pause_details = $1::jsonb,
                   resume_eligible_at = NULL,
                   lease_owner = NULL,
                   lease_expiry = NULL,
                   version = version + 1,
                   updated_at = NOW()
               WHERE task_id = $2::uuid""",
            pause_details, task_id,
        )

        # Decrement running_task_count
        await e2e.db.execute(
            """INSERT INTO agent_runtime_state
                   (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, updated_at)
               VALUES ($1, $2, 0, $3, NOW())
               ON CONFLICT (tenant_id, agent_id) DO UPDATE
               SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                   hour_window_cost_microdollars = agent_runtime_state.hour_window_cost_microdollars + $3,
                   updated_at = NOW()""",
            TENANT_ID, agent_id, observed_cost,
        )

        # Record task_paused event
        event_details = json.dumps({
            "pause_reason": "budget_per_task",
            "budget_max_per_task": budget_max_per_task,
            "observed_task_cost_microdollars": observed_cost,
            "recovery_mode": "manual_resume_after_budget_increase",
        })
        await e2e.db.execute(
            """INSERT INTO task_events
                   (tenant_id, task_id, agent_id, event_type, status_before,
                    status_after, details, created_at)
               VALUES ($1, $2::uuid, $3, 'task_paused', 'running', 'paused',
                       $4::jsonb, NOW())""",
            TENANT_ID, task_id, agent_id, event_details,
        )

    async def _simulate_hourly_budget_pause(self, e2e, agent_id, task_id,
                                             budget_max_per_hour, observed_cost):
        """Simulate an hourly budget pause via DB manipulation."""
        pause_details = json.dumps({
            "budget_max_per_hour": budget_max_per_hour,
            "observed_hour_cost_microdollars": observed_cost,
            "recovery_mode": "automatic_after_window_clears",
        })

        # Insert cost into the ledger
        await e2e.db.execute(
            """INSERT INTO agent_cost_ledger
                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
               VALUES ($1, $2, $3::uuid, $4, $5)""",
            TENANT_ID, agent_id, task_id, str(uuid.uuid4()), observed_cost,
        )

        # Transition task to paused with resume_eligible_at
        await e2e.db.execute(
            """UPDATE tasks
               SET status = 'paused',
                   pause_reason = 'budget_per_hour',
                   pause_details = $1::jsonb,
                   resume_eligible_at = NOW() + INTERVAL '60 minutes',
                   lease_owner = NULL,
                   lease_expiry = NULL,
                   version = version + 1,
                   updated_at = NOW()
               WHERE task_id = $2::uuid""",
            pause_details, task_id,
        )

        # Update runtime state
        await e2e.db.execute(
            """INSERT INTO agent_runtime_state
                   (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, updated_at)
               VALUES ($1, $2, 0, $3, NOW())
               ON CONFLICT (tenant_id, agent_id) DO UPDATE
               SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                   hour_window_cost_microdollars = $3,
                   updated_at = NOW()""",
            TENANT_ID, agent_id, observed_cost,
        )

        # Record task_paused event
        event_details = json.dumps({
            "pause_reason": "budget_per_hour",
            "budget_max_per_hour": budget_max_per_hour,
            "observed_hour_cost_microdollars": observed_cost,
            "recovery_mode": "automatic_after_window_clears",
        })
        await e2e.db.execute(
            """INSERT INTO task_events
                   (tenant_id, task_id, agent_id, event_type, status_before,
                    status_after, details, created_at)
               VALUES ($1, $2::uuid, $3, 'task_paused', 'running', 'paused',
                       $4::jsonb, NOW())""",
            TENANT_ID, task_id, agent_id, event_details,
        )

    async def test_per_task_budget_pause(self, e2e):
        """Task exceeding per-task budget pauses with correct reason."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-pertask-budget-worker")

        budget_limit = 1000  # 1000 microdollars
        agent = e2e.ensure_agent(
            agent_id="budget-pertask-agent",
            display_name="Budget PerTask Agent",
            budget_max_per_task=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)

        # Wait for task to start running (or complete)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        # Simulate cost exceeding per-task budget
        await self._simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=budget_limit,
            observed_cost=budget_limit + 500,
        )

        # Verify task is paused via API
        task = e2e.get_task(task_id)
        assert task["status"] == "paused"
        assert task["pause_reason"] == "budget_per_task"
        assert task["pause_details"]["recovery_mode"] == "manual_resume_after_budget_increase"
        assert task.get("resume_eligible_at") is None

    async def test_hourly_budget_pause(self, e2e):
        """Agent exceeding hourly budget pauses tasks with auto-recovery."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-hourly-budget-worker")

        budget_limit = 1000
        agent = e2e.ensure_agent(
            agent_id="budget-hourly-agent",
            display_name="Budget Hourly Agent",
            budget_max_per_hour=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        await self._simulate_hourly_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_hour=budget_limit,
            observed_cost=budget_limit + 500,
        )

        task = e2e.get_task(task_id)
        assert task["status"] == "paused"
        assert task["pause_reason"] == "budget_per_hour"
        assert task["pause_details"]["recovery_mode"] == "automatic_after_window_clears"
        assert task["resume_eligible_at"] is not None

    async def test_budget_precedence(self, e2e):
        """When both budgets exceeded, per-task takes precedence."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-precedence-worker")

        agent = e2e.ensure_agent(
            agent_id="budget-precedence-agent",
            display_name="Budget Precedence Agent",
            budget_max_per_task=1000,
            budget_max_per_hour=1000,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        # Simulate per-task budget pause (which takes precedence)
        await self._simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=1000,
            observed_cost=1500,
        )

        task = e2e.get_task(task_id)
        assert task["status"] == "paused"
        assert task["pause_reason"] == "budget_per_task", (
            f"Expected budget_per_task to take precedence, got {task['pause_reason']}"
        )

    async def test_hourly_budget_blocks_new_claims(self, e2e):
        """Agent over hourly budget -- new tasks remain queued.

        The scheduler checks hour_window_cost_microdollars < budget_max_per_hour
        before allowing a claim. Setting the hourly cost above budget should
        prevent new claims.
        """
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-hourly-block-worker")

        budget_limit = 100  # Very low hourly budget
        agent = e2e.ensure_agent(
            agent_id="hourly-block-agent",
            display_name="Hourly Block Agent",
            budget_max_per_hour=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        # Set the hourly cost above the budget in agent_runtime_state
        # so the scheduler will reject claims for this agent
        await e2e.db.execute(
            """INSERT INTO agent_runtime_state
                   (tenant_id, agent_id, running_task_count,
                    hour_window_cost_microdollars, scheduler_cursor, updated_at)
               VALUES ($1, $2, 0, $3, NOW(), NOW())
               ON CONFLICT (tenant_id, agent_id) DO UPDATE
               SET hour_window_cost_microdollars = $3, updated_at = NOW()""",
            TENANT_ID, agent_id, budget_limit + 1000,
        )

        # Also insert a cost ledger entry to back up the runtime state cache
        await e2e.db.execute(
            """INSERT INTO agent_cost_ledger
                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
               VALUES ($1, $2, $3::uuid, $4, $5)""",
            TENANT_ID, agent_id, str(uuid.uuid4()), str(uuid.uuid4()),
            budget_limit + 1000,
        )

        # Submit a new task -- it should stay queued
        task_id = e2e.submit_task(agent_id=agent_id)
        await asyncio.sleep(5)

        task = e2e.get_task(task_id)
        assert task["status"] == "queued", (
            f"Expected task to remain queued due to hourly budget, got {task['status']}"
        )

    async def test_task_paused_event_details(self, e2e):
        """Verify task_paused event has correct budget details schema."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-event-details-worker")

        budget_limit = 1000
        agent = e2e.ensure_agent(
            agent_id="event-details-agent",
            display_name="Event Details Agent",
            budget_max_per_task=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        observed_cost = budget_limit + 500
        await self._simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=budget_limit,
            observed_cost=observed_cost,
        )

        events = e2e.get_events(task_id)
        pause_events = [e for e in events if e["event_type"] == "task_paused"]
        assert len(pause_events) >= 1, f"Expected at least one task_paused event, got {len(pause_events)}"

        details = pause_events[0]["details"]
        assert details["pause_reason"] == "budget_per_task"
        assert "budget_max_per_task" in details
        assert details["budget_max_per_task"] == budget_limit
        assert "observed_task_cost_microdollars" in details
        assert details["observed_task_cost_microdollars"] == observed_cost
        assert details["recovery_mode"] == "manual_resume_after_budget_increase"
