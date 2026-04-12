"""Integration tests for Track 3 agent budget CRUD operations.

Tests that agent create/update operations correctly handle the new
scheduler and budget fields (max_concurrent_tasks, budget_max_per_task,
budget_max_per_hour), and that task list filtering by pause_reason works.
"""

import asyncio
import json
import uuid

import pytest

from helpers.mock_llm import simple_response


TENANT_ID = "default"

DEFAULT_CONFIG = {
    "system_prompt": "You are a test assistant.",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "temperature": 0.5,
    "allowed_tools": ["web_search"],
}


class TestAgentBudgetCRUD:

    def test_create_agent_with_budget_fields(self, e2e):
        """Agent created with custom budget/concurrency settings."""
        resp = e2e.api.create_agent(
            display_name="Budget CRUD Agent",
            agent_config=DEFAULT_CONFIG,
            max_concurrent_tasks=3,
            budget_max_per_task=1000000,
            budget_max_per_hour=10000000,
        )
        assert resp["status_code"] == 201
        agent = resp["body"]
        assert agent["max_concurrent_tasks"] == 3
        assert agent["budget_max_per_task"] == 1000000
        assert agent["budget_max_per_hour"] == 10000000

    def test_create_agent_defaults(self, e2e):
        """Agent created without budget fields gets defaults."""
        resp = e2e.api.create_agent(
            display_name="Default Budget Agent",
            agent_config=DEFAULT_CONFIG,
        )
        assert resp["status_code"] == 201
        agent = resp["body"]
        # Default values from migration 0007
        assert agent["max_concurrent_tasks"] == 5
        assert agent["budget_max_per_task"] == 500000
        assert agent["budget_max_per_hour"] == 5000000

    def test_update_agent_budget(self, e2e):
        """Agent budget fields can be updated via partial update."""
        create_resp = e2e.api.create_agent(
            display_name="Update Budget Agent",
            agent_config=DEFAULT_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        # Update only budget_max_per_task using kwargs-based partial update
        resp = e2e.api.update_agent(agent_id, budget_max_per_task=2000000)
        assert resp["status_code"] == 200
        updated = resp["body"]
        assert updated["budget_max_per_task"] == 2000000
        # Other budget fields should remain at defaults
        assert updated["max_concurrent_tasks"] == 5
        assert updated["budget_max_per_hour"] == 5000000

    def test_update_agent_multiple_budget_fields(self, e2e):
        """Multiple budget fields can be updated simultaneously."""
        create_resp = e2e.api.create_agent(
            display_name="Multi Budget Agent",
            agent_config=DEFAULT_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        resp = e2e.api.update_agent(
            agent_id,
            max_concurrent_tasks=10,
            budget_max_per_task=3000000,
            budget_max_per_hour=15000000,
        )
        assert resp["status_code"] == 200
        updated = resp["body"]
        assert updated["max_concurrent_tasks"] == 10
        assert updated["budget_max_per_task"] == 3000000
        assert updated["budget_max_per_hour"] == 15000000

    def test_get_agent_includes_budget_fields(self, e2e):
        """GET /agents/{id} includes budget fields in response."""
        create_resp = e2e.api.create_agent(
            display_name="Get Budget Agent",
            agent_config=DEFAULT_CONFIG,
            max_concurrent_tasks=7,
            budget_max_per_task=999999,
            budget_max_per_hour=8888888,
        )
        agent_id = create_resp["body"]["agent_id"]

        resp = e2e.api.get_agent(agent_id)
        assert resp["status_code"] == 200
        body = resp["body"]
        assert body["max_concurrent_tasks"] == 7
        assert body["budget_max_per_task"] == 999999
        assert body["budget_max_per_hour"] == 8888888


@pytest.mark.asyncio
class TestTaskListPauseReasonFilter:

    async def test_task_list_pause_reason_filter(self, e2e):
        """Task list filters by pause_reason."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-filter-worker")

        agent = e2e.ensure_agent(
            agent_id="filter-pause-agent",
            display_name="Filter Agent",
            budget_max_per_task=1000,
        )
        agent_id = agent["body"]["agent_id"]

        task_id = e2e.submit_task(agent_id=agent_id)
        await e2e.wait_for_statuses(
            task_id, {"running", "completed"}, timeout=30,
        )

        # Simulate per-task budget pause via DB
        pause_details = json.dumps({
            "budget_max_per_task": 1000,
            "observed_task_cost_microdollars": 1500,
            "recovery_mode": "manual_resume_after_budget_increase",
        })
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

        # Filter by pause_reason=budget_per_task -- should include our task
        resp = e2e.api.list_tasks(status="paused", pause_reason="budget_per_task")
        assert resp["status_code"] == 200
        items = resp["body"].get("items", resp["body"].get("tasks", []))
        matching = [t for t in items if t["task_id"] == task_id]
        assert len(matching) >= 1, (
            f"Expected task {task_id} in paused tasks filtered by budget_per_task"
        )

        # Filter by different pause_reason -- should not include our task
        resp = e2e.api.list_tasks(status="paused", pause_reason="budget_per_hour")
        assert resp["status_code"] == 200
        items = resp["body"].get("items", resp["body"].get("tasks", []))
        matching = [t for t in items if t["task_id"] == task_id]
        assert len(matching) == 0, (
            f"Expected task {task_id} NOT in tasks filtered by budget_per_hour"
        )
