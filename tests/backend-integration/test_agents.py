"""Dedicated agent CRUD integration tests for Track 1."""

import pytest

from helpers.api_client import ApiError
from helpers.mock_llm import simple_response


DEFAULT_CONFIG = {
    "system_prompt": "You are a test assistant.",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "temperature": 0.5,
    "allowed_tools": ["web_search"],
}


class TestAgentCRUD:
    def test_create_agent(self, e2e):
        """POST /v1/agents creates agent with auto-generated ID, returns 201."""
        resp = e2e.api.create_agent(
            display_name="CRUD Create Agent",
            agent_config=DEFAULT_CONFIG,
        )
        assert resp["status_code"] == 201
        body = resp["body"]
        assert body["agent_id"]  # auto-generated UUID
        assert body["display_name"] == "CRUD Create Agent"
        assert body["status"] == "active"
        assert "created_at" in body
        assert "updated_at" in body

    def test_list_agents(self, e2e):
        """GET /v1/agents returns agent list."""
        resp_a = e2e.api.create_agent(display_name="Agent A")
        resp_b = e2e.api.create_agent(display_name="Agent B")
        agent_a_id = resp_a["body"]["agent_id"]
        agent_b_id = resp_b["body"]["agent_id"]

        resp = e2e.api.list_agents()
        assert resp["status_code"] == 200
        body = resp["body"]
        items = body if isinstance(body, list) else body.get("items", body.get("agents", []))
        agent_ids = {a["agent_id"] for a in items}
        assert agent_a_id in agent_ids
        assert agent_b_id in agent_ids

    def test_list_agents_status_filter(self, e2e):
        """GET /v1/agents?status=active filters correctly."""
        resp_active = e2e.api.create_agent(display_name="Active")
        resp_disabled = e2e.api.create_agent(display_name="Disabled")
        active_id = resp_active["body"]["agent_id"]
        disabled_id = resp_disabled["body"]["agent_id"]

        e2e.api.update_agent(
            disabled_id,
            display_name="Disabled",
            agent_config=DEFAULT_CONFIG,
            status="disabled",
        )
        resp = e2e.api.list_agents(status="active")
        body = resp["body"]
        items = body if isinstance(body, list) else body.get("items", body.get("agents", []))
        agent_ids = {a["agent_id"] for a in items}
        assert active_id in agent_ids
        assert disabled_id not in agent_ids

    def test_get_agent_detail(self, e2e):
        """GET /v1/agents/{id} returns full config."""
        resp = e2e.api.create_agent(display_name="Detail Agent")
        agent_id = resp["body"]["agent_id"]

        resp = e2e.api.get_agent(agent_id)
        assert resp["status_code"] == 200
        body = resp["body"]
        assert body["agent_id"] == agent_id
        assert body["display_name"] == "Detail Agent"
        assert "agent_config" in body

    def test_get_agent_not_found_returns_404(self, e2e):
        """GET /v1/agents/{unknown} returns 404."""
        resp = e2e.api._request("GET", "/agents/nonexistent_agent_xyz", expected_status=(200, 404), raise_for_status=False)
        assert resp["status_code"] == 404

    def test_update_agent(self, e2e):
        """PUT /v1/agents/{id} updates and returns updated agent."""
        resp = e2e.api.create_agent(display_name="Before")
        agent_id = resp["body"]["agent_id"]

        updated_config = {**DEFAULT_CONFIG, "temperature": 0.9}
        resp = e2e.api.update_agent(
            agent_id,
            display_name="After",
            agent_config=updated_config,
            status="active",
        )
        assert resp["status_code"] == 200
        body = resp["body"]
        assert body["display_name"] == "After"

    def test_update_agent_not_found_returns_404(self, e2e):
        """PUT /v1/agents/{unknown} returns 404."""
        resp = e2e.api._request(
            "PUT", "/agents/nonexistent_agent_xyz",
            {"display_name": "X", "agent_config": DEFAULT_CONFIG, "status": "active"},
            expected_status=(200, 404),
            raise_for_status=False,
        )
        assert resp["status_code"] == 404

    def test_submit_task_with_disabled_agent_returns_400(self, e2e):
        """POST /v1/tasks with disabled agent returns 400."""
        resp = e2e.api.create_agent(display_name="Disabled Agent")
        agent_id = resp["body"]["agent_id"]

        e2e.api.update_agent(
            agent_id,
            display_name="Disabled Agent",
            agent_config=DEFAULT_CONFIG,
            status="disabled",
        )
        resp = e2e.api.submit_task(
            agent_id=agent_id,
            input="should fail",
            expected_status=(400, 422),
            raise_for_status=False,
        )
        assert resp["status_code"] in (400, 422)

    def test_submit_task_with_unknown_agent_returns_404(self, e2e):
        """POST /v1/tasks with unknown agent_id returns 404."""
        resp = e2e.api.submit_task(
            agent_id="totally_unknown_agent",
            input="should fail",
            expected_status=(404, 400),
            raise_for_status=False,
        )
        assert resp["status_code"] in (404, 400)

    @pytest.mark.asyncio
    async def test_submit_task_snapshots_display_name(self, e2e):
        """POST /v1/tasks snapshots display_name, visible in GET /v1/tasks/{id}."""
        resp = e2e.api.create_agent(
            display_name="Snapshot Agent",
            agent_config=DEFAULT_CONFIG,
        )
        agent_id = resp["body"]["agent_id"]

        resp = e2e.api.submit_task(agent_id=agent_id, input="snapshot test")
        assert resp["status_code"] == 201
        task_id = resp["body"]["task_id"]

        task = e2e.api.get_task(task_id)["body"]
        assert task["agent_id"] == agent_id
        assert task.get("agent_display_name") == "Snapshot Agent"

    @pytest.mark.asyncio
    async def test_agent_edit_does_not_affect_existing_task(self, e2e):
        """Editing agent after task submission doesn't change task snapshot."""
        resp = e2e.api.create_agent(
            display_name="Original Name",
            agent_config=DEFAULT_CONFIG,
        )
        agent_id = resp["body"]["agent_id"]

        resp = e2e.api.submit_task(agent_id=agent_id, input="isolation test")
        task_id = resp["body"]["task_id"]

        # Edit agent display name
        e2e.api.update_agent(
            agent_id,
            display_name="Updated Name",
            agent_config=DEFAULT_CONFIG,
            status="active",
        )

        # Task should still show original display name
        task = e2e.api.get_task(task_id)["body"]
        assert task.get("agent_display_name") == "Original Name"
