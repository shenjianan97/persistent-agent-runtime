import os

import pytest

from helpers.mock_llm import callback_friendly_response


@pytest.mark.asyncio
async def test_langfuse_observability_smoke(e2e):
    e2e.use_llm(callback_friendly_response("Observability ready"))
    await e2e.start_worker(
        "e2e-langfuse-worker",
        config_overrides={
            "langfuse_enabled": True,
            "langfuse_host": os.getenv("LANGFUSE_HOST", "http://127.0.0.1:3300"),
            "langfuse_public_key": os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-local"),
            "langfuse_secret_key": os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-local"),
        },
    )

    task_id = e2e.submit_task(allowed_tools=[], input="Say hello for observability")
    completed = await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    async def load_observability():
        response = e2e.api.get_observability(task_id)["body"]
        return response if response.get("trace_id") else None

    observability = await e2e.wait_for(
        load_observability,
        timeout=20.0,
        description="Langfuse observability trace",
    )

    assert completed["status"] == "completed"
    assert completed["total_cost_microdollars"] >= 0
    assert observability["enabled"] is True
    assert observability["trace_id"]
    assert observability["items"]
