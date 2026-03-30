import os

import pytest

from helpers.mock_llm import callback_friendly_response


@pytest.mark.asyncio
async def test_langfuse_observability_smoke(e2e):
    e2e.use_llm(callback_friendly_response("Observability ready"))

    # 1. Create a Langfuse endpoint via the API
    endpoint_resp = e2e.api._request(
        "POST",
        "/langfuse-endpoints",
        payload={
            "name": "e2e-test-langfuse",
            "host": os.getenv("LANGFUSE_HOST", "http://127.0.0.1:3300"),
            "public_key": os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-local"),
            "secret_key": os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-local"),
        },
        expected_status=201,
    )
    endpoint_id = endpoint_resp["body"]["endpoint_id"]

    await e2e.start_worker("e2e-langfuse-worker")

    # 2. Submit a task with langfuse_endpoint_id
    task_id = e2e.submit_task(
        allowed_tools=[],
        input="Say hello for observability",
        langfuse_endpoint_id=endpoint_id,
    )

    # 3. Wait for task completion
    completed = await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    # 4. Verify checkpoint cost data
    async def load_observability():
        response = e2e.api.get_observability(task_id)["body"]
        if response.get("items") and len(response["items"]) > 0:
            return response
        return None

    observability = await e2e.wait_for(
        load_observability,
        timeout=20.0,
        description="Checkpoint-based observability data",
    )

    assert completed["status"] == "completed"
    assert completed["total_cost_microdollars"] >= 0
    assert observability["items"]
