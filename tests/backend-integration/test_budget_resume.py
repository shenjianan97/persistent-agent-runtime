"""Integration tests for Track 3 budget resume flows.

Tests manual resume after budget increase, error cases (non-paused, nonexistent,
disabled agent), event details and ordering, and hourly auto-recovery.

Since mock LLM responses have zero cost, budget pauses are simulated via DB
manipulation (same pattern as test_budget_enforcement.py).
"""

import asyncio
import json
import uuid

import pytest

from helpers.api_client import ApiError
from helpers.mock_llm import simple_response


TENANT_ID = "default"


async def _simulate_per_task_budget_pause(e2e, agent_id, task_id,
                                           budget_max_per_task, observed_cost):
    """Simulate a per-task budget pause by directly manipulating the DB."""
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

    # Update runtime state
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


async def _simulate_hourly_budget_pause(e2e, agent_id, task_id,
                                         budget_max_per_hour, observed_cost):
    """Simulate an hourly budget pause via DB manipulation."""
    pause_details = json.dumps({
        "budget_max_per_hour": budget_max_per_hour,
        "observed_hour_cost_microdollars": observed_cost,
        "recovery_mode": "automatic_after_window_clears",
    })

    await e2e.db.execute(
        """INSERT INTO agent_cost_ledger
               (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
           VALUES ($1, $2, $3::uuid, $4, $5)""",
        TENANT_ID, agent_id, task_id, str(uuid.uuid4()), observed_cost,
    )

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


@pytest.mark.asyncio
class TestBudgetResume:

    async def test_manual_resume_after_budget_increase(self, e2e):
        """Per-task budget pause -> increase budget -> resume -> task completes."""
        e2e.use_llm(simple_response("resumed and done"))
        await e2e.start_worker("e2e-resume-worker")

        budget_limit = 1000
        observed_cost = 1500
        agent = e2e.ensure_agent(
            agent_id="resume-increase-agent",
            display_name="Resume Increase Agent",
            budget_max_per_task=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        # Simulate budget pause
        await _simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=budget_limit,
            observed_cost=observed_cost,
        )

        # Verify paused
        task = e2e.get_task(task_id)
        assert task["status"] == "paused"

        # Resume while still over budget -> should fail with 409
        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task(task_id)
        assert exc_info.value.status_code == 409

        # Increase budget well above observed cost
        e2e.api.update_agent(agent_id, budget_max_per_task=100_000_000)

        # Resume -> should succeed
        result = e2e.resume_task(task_id)
        assert result["status"] == "queued"

        # Task should eventually complete
        await e2e.wait_for_status(task_id, "completed", timeout=60)

    async def test_resume_non_paused_task(self, e2e):
        """Resume on a queued task returns 409."""
        e2e.use_llm(simple_response("done"))

        agent = e2e.ensure_agent(
            agent_id="resume-wrong-state-agent",
            display_name="Resume Wrong State Agent",
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)

        # Task is queued (or may start running quickly) -- resume should fail
        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task(task_id)
        assert exc_info.value.status_code == 409

    async def test_resume_nonexistent_task(self, e2e):
        """Resume on a nonexistent task returns 404."""
        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task("00000000-0000-0000-0000-000000000000")
        assert exc_info.value.status_code == 404

    async def test_resume_disabled_agent_rejected(self, e2e):
        """Resume rejected when agent is disabled."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-resume-disabled-worker")

        budget_limit = 1000
        observed_cost = 1500
        agent = e2e.ensure_agent(
            agent_id="resume-disabled-agent",
            display_name="Resume Disabled Agent",
            budget_max_per_task=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        await _simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=budget_limit,
            observed_cost=observed_cost,
        )

        # Disable agent
        e2e.api.update_agent(agent_id, status="disabled")

        # Increase budget but agent is disabled
        e2e.api.update_agent(agent_id, budget_max_per_task=100_000_000)

        with pytest.raises(ApiError) as exc_info:
            e2e.resume_task(task_id)
        assert exc_info.value.status_code == 409

    async def test_task_resumed_event_details(self, e2e):
        """Verify task_resumed event has correct details after manual resume."""
        e2e.use_llm(simple_response("resumed ok"))
        await e2e.start_worker("e2e-resume-events-worker")

        budget_limit = 1000
        observed_cost = 1500
        new_budget = 100_000_000
        agent = e2e.ensure_agent(
            agent_id="resume-events-agent",
            display_name="Resume Events Agent",
            budget_max_per_task=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        await _simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=budget_limit,
            observed_cost=observed_cost,
        )

        e2e.api.update_agent(agent_id, budget_max_per_task=new_budget)
        e2e.resume_task(task_id)

        # Wait briefly for event to be recorded
        await asyncio.sleep(1)

        events = e2e.get_events(task_id)
        resume_events = [e for e in events if e["event_type"] == "task_resumed"]
        assert len(resume_events) >= 1, (
            f"Expected at least one task_resumed event, got {len(resume_events)}"
        )

        details = resume_events[0]["details"]
        assert details["resume_trigger"] == "manual_operator_resume"
        assert "budget_max_per_task_at_resume" in details

    async def test_correct_event_ordering(self, e2e):
        """Events follow: submitted -> claimed -> paused -> resumed -> claimed -> completed."""
        e2e.use_llm(simple_response("final done"))
        await e2e.start_worker("e2e-event-order-worker")

        budget_limit = 1000
        observed_cost = 1500
        agent = e2e.ensure_agent(
            agent_id="event-order-agent",
            display_name="Event Order Agent",
            budget_max_per_task=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)

        # Wait for task to start running first
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        # Simulate budget pause
        await _simulate_per_task_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_task=budget_limit,
            observed_cost=observed_cost,
        )

        # Increase budget and resume
        e2e.api.update_agent(agent_id, budget_max_per_task=100_000_000)
        e2e.resume_task(task_id)

        # Wait for task to complete after resume
        await e2e.wait_for_status(task_id, "completed", timeout=60)

        events = e2e.get_events(task_id)
        event_types = [e["event_type"] for e in events]

        # Core events that must be present
        assert "task_claimed" in event_types, f"Missing task_claimed. Events: {event_types}"
        assert "task_paused" in event_types, f"Missing task_paused. Events: {event_types}"
        assert "task_resumed" in event_types, f"Missing task_resumed. Events: {event_types}"
        assert "task_completed" in event_types, f"Missing task_completed. Events: {event_types}"

        # Verify relative ordering: paused comes after first claimed, resumed comes after paused
        claimed_idx = event_types.index("task_claimed")
        paused_idx = event_types.index("task_paused")
        resumed_idx = event_types.index("task_resumed")
        assert claimed_idx < paused_idx < resumed_idx, (
            f"Expected claimed < paused < resumed, got "
            f"claimed={claimed_idx} paused={paused_idx} resumed={resumed_idx}. "
            f"Events: {event_types}"
        )

    async def test_hourly_auto_resume(self, e2e):
        """Hourly budget pause auto-recovers after rolling window clears.

        This test simulates an hourly budget pause, then manipulates the DB to
        make it look like the rolling window has cleared, and verifies the
        reaper auto-recovers the task.
        """
        e2e.use_llm(simple_response("auto resumed"))
        await e2e.start_worker("e2e-auto-resume-worker")

        budget_limit = 1000
        observed_cost = 1500
        agent = e2e.ensure_agent(
            agent_id="auto-resume-agent",
            display_name="Auto Resume Agent",
            budget_max_per_hour=budget_limit,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        # Simulate hourly budget pause
        await _simulate_hourly_budget_pause(
            e2e, agent_id, task_id,
            budget_max_per_hour=budget_limit,
            observed_cost=observed_cost,
        )

        task = e2e.get_task(task_id)
        assert task["status"] == "paused"
        assert task["pause_reason"] == "budget_per_hour"

        # Simulate time passing: delete old cost ledger entries and
        # update resume_eligible_at to the past, clear hourly cache
        await e2e.db.execute(
            "DELETE FROM agent_cost_ledger WHERE tenant_id = $1 AND agent_id = $2",
            TENANT_ID, agent_id,
        )
        await e2e.db.execute(
            """UPDATE tasks SET resume_eligible_at = NOW() - INTERVAL '1 minute'
               WHERE task_id = $1::uuid""",
            task_id,
        )
        await e2e.db.execute(
            """UPDATE agent_runtime_state
               SET hour_window_cost_microdollars = 0, updated_at = NOW()
               WHERE tenant_id = $1 AND agent_id = $2""",
            TENANT_ID, agent_id,
        )

        # Wait for reaper cycle to pick up the auto-recovery
        # The reaper runs every ~5 seconds in test config
        await e2e.wait_for_statuses(
            task_id, {"queued", "running", "completed"}, timeout=120,
        )

        # Verify task_resumed event was emitted by the reaper
        events = e2e.get_events(task_id)
        resume_events = [e for e in events if e["event_type"] == "task_resumed"]
        assert len(resume_events) >= 1, (
            f"Expected at least one task_resumed event after auto-recovery, "
            f"got {len(resume_events)}"
        )
        # At least one resume should be automatic
        auto_resumes = [
            e for e in resume_events
            if e.get("details", {}).get("resume_trigger") == "automatic_hourly_recovery"
        ]
        assert len(auto_resumes) >= 1, (
            f"Expected automatic_hourly_recovery resume trigger, "
            f"got {[e.get('details', {}).get('resume_trigger') for e in resume_events]}"
        )
