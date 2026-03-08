import pytest


@pytest.mark.asyncio
async def test_3_27_tenant_scoping(e2e):
    """3.27 API queries should remain scoped to tenant_id='default' in Phase 1."""
    assert e2e.submit_task(input="default tenant task")

    other_task = await e2e.db.insert_task(
        tenant_id="other_tenant",
        status="dead_letter",
        dead_letter_reason="non_retryable_error",
        agent_id="other_agent",
    )

    dead_letters = e2e.api.get_dead_letters(limit=100)["body"]["items"]
    returned_ids = {item["task_id"] for item in dead_letters}
    assert other_task not in returned_ids

    response = e2e.api.get_task(other_task, expected_status=404, raise_for_status=False)
    assert response["status_code"] == 404
