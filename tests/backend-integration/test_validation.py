import pytest

from helpers.mock_llm import simple_response


@pytest.mark.asyncio
async def test_3_15_input_validation_matrix(e2e):
    """3.15 Validate API rejection paths and state-transition conflicts."""
    e2e.ensure_agent()

    # Task-level validation cases (these are still validated at task submission time)
    task_bad_cases = [
        {"agent_id": ""},
        {"input": "x" * 102401},
        {"task_timeout_seconds": 100000},
        {"max_steps": 0},
    ]

    for payload in task_bad_cases:
        response = e2e.api.submit_task(**payload, expected_status=400, raise_for_status=False)
        assert response["status_code"] == 400

    # Agent-level config validation cases (now validated at agent creation time)
    from helpers.api_client import ApiError
    agent_bad_cases = [
        {"agent_config": {"system_prompt": "", "provider": "anthropic", "model": "claude-sonnet-4-6", "temperature": 0.5, "allowed_tools": ["calculator"]}},
        {"agent_config": {"system_prompt": "test", "provider": "anthropic", "model": "gpt-5-ultra", "temperature": 0.5, "allowed_tools": ["calculator"]}},
        {"agent_config": {"system_prompt": "test", "provider": "anthropic", "model": "claude-sonnet-4-6", "temperature": 3.0, "allowed_tools": ["calculator"]}},
        {"agent_config": {"system_prompt": "test", "provider": "anthropic", "model": "claude-sonnet-4-6", "temperature": 0.5, "allowed_tools": ["rm_rf"]}},
    ]

    for idx, overrides in enumerate(agent_bad_cases):
        response = e2e.api.create_agent(
            agent_id=f"bad_agent_{idx}",
            display_name="Bad Agent",
            expected_status=(400, 201),
            raise_for_status=False,
            **overrides,
        )
        # These should be rejected (400) by the API
        assert response["status_code"] == 400, f"Expected 400 for agent_bad_cases[{idx}], got {response['status_code']}"

    not_found = e2e.api.get_task(
        "11111111-1111-1111-1111-111111111111",
        expected_status=404,
        raise_for_status=False,
    )
    assert not_found["status_code"] == 404

    e2e.use_llm(simple_response("done"))
    await e2e.start_worker("e2e-validation")

    completed_id = e2e.submit_task(input="complete me")
    await e2e.wait_for_status(completed_id, "completed", timeout=20.0)

    cancel_completed = e2e.api.cancel_task(completed_id, expected_status=409, raise_for_status=False)
    assert cancel_completed["status_code"] == 409

    queued_id = e2e.submit_task(input="queued")
    redrive_non_dl = e2e.api.redrive_task(queued_id, expected_status=409, raise_for_status=False)
    assert redrive_non_dl["status_code"] == 409
