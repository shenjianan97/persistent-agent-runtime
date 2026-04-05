"""Integration tests for Track 3 scheduler fairness.

Tests round-robin scheduling across agents, concurrency cap enforcement,
and disabled agent behavior.
"""

import asyncio

import pytest

from helpers.mock_llm import simple_response, slow_response


@pytest.mark.asyncio
class TestSchedulerFairness:

    async def test_round_robin_across_agents(self, e2e):
        """Two agents with queued tasks -- claims should alternate between them."""
        e2e.use_llm(simple_response("done"))
        await e2e.start_worker("e2e-fairness-worker")

        agent_a = e2e.ensure_agent(agent_id="fairness-agent-a", display_name="Fairness A")
        agent_b = e2e.ensure_agent(agent_id="fairness-agent-b", display_name="Fairness B")

        agent_a_id = agent_a["body"]["agent_id"]
        agent_b_id = agent_b["body"]["agent_id"]

        # Submit 4 tasks for each agent
        tasks_a = [e2e.submit_task(agent_id=agent_a_id) for _ in range(4)]
        tasks_b = [e2e.submit_task(agent_id=agent_b_id) for _ in range(4)]

        # Wait for all tasks to complete
        for t in tasks_a + tasks_b:
            await e2e.wait_for_status(t, "completed", timeout=60)

        # Collect claim events with timestamps for both agents
        all_claim_events = []
        for t in tasks_a:
            events = e2e.get_events(t)
            claim_events = [e for e in events if e["event_type"] == "task_claimed"]
            all_claim_events.extend([(e["created_at"], "a") for e in claim_events])
        for t in tasks_b:
            events = e2e.get_events(t)
            claim_events = [e for e in events if e["event_type"] == "task_claimed"]
            all_claim_events.extend([(e["created_at"], "b") for e in claim_events])

        all_claim_events.sort()  # Sort by claim time

        # Verify fairness: first 4 claims should include both agents
        first_4_agents = {agent for _, agent in all_claim_events[:4]}
        assert len(first_4_agents) == 2, (
            f"Expected both agents in first 4 claims, got {first_4_agents}. "
            f"Claim order: {[(t, a) for t, a in all_claim_events[:4]]}"
        )

    async def test_concurrency_cap_enforcement(self, e2e):
        """Agent with max_concurrent_tasks=1 -- only one task runs at a time.

        Uses a slow response to keep the first task in 'running' state long
        enough to verify that additional tasks remain queued.
        """
        # Use slow_response so the first task stays in "running" for a while
        e2e.use_llm(slow_response(delay=10.0, content="done"))
        await e2e.start_worker("e2e-concurrency-worker")

        agent = e2e.ensure_agent(
            agent_id="concurrency-cap-agent",
            display_name="Concurrency Agent",
            max_concurrent_tasks=1,
        )
        agent_id = agent["body"]["agent_id"]

        # Submit 3 tasks
        task_ids = [e2e.submit_task(agent_id=agent_id) for _ in range(3)]

        # Wait for one task to be running
        await e2e.wait_for_statuses(
            task_ids[0], {"running", "completed"}, timeout=30,
        )

        # Give scheduler a moment to process all tasks
        await asyncio.sleep(2)

        # At this point, at most 1 task should be running
        statuses = [e2e.get_task(t)["status"] for t in task_ids]
        running_count = sum(1 for s in statuses if s == "running")
        assert running_count <= 1, f"Expected at most 1 running, got {running_count}. Statuses: {statuses}"

    async def test_disabled_agent_not_scheduled(self, e2e):
        """Disabled agent's tasks are not claimed."""
        e2e.use_llm(simple_response("done"))

        # Create agent as active, then disable it BEFORE starting the worker
        # to avoid race conditions where the worker claims the task before disable
        agent = e2e.ensure_agent(
            agent_id="disabled-sched-agent",
            display_name="Disabled Agent",
        )
        agent_id = agent["body"]["agent_id"]

        # Submit a task while agent is still active (API requires active agent)
        task_id = e2e.submit_task(agent_id=agent_id)

        # Disable the agent BEFORE starting the worker
        e2e.api.update_agent(agent_id, status="disabled")

        # Now start the worker -- it should skip the disabled agent's tasks
        await e2e.start_worker("e2e-disabled-worker")

        # Wait briefly -- task should remain queued because agent is disabled
        await asyncio.sleep(5)
        task = e2e.get_task(task_id)
        assert task["status"] == "queued", (
            f"Expected task to remain queued for disabled agent, got {task['status']}"
        )
