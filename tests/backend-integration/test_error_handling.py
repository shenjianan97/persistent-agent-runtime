import pytest

from helpers.mock_llm import always_fails, infinite_tool_loop, slow_response


@pytest.mark.asyncio
async def test_3_8_non_retryable_error_immediate_dead_letter(e2e):
    """3.8 Non-retryable (400) should dead-letter without retries."""
    e2e.use_llm(always_fails("400 Bad Request: invalid prompt"))
    await e2e.start_worker("e2e-non-retryable")

    e2e.ensure_agent()
    task_id = e2e.submit_task(input="bad request")
    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=20.0)

    assert dead["dead_letter_reason"] == "non_retryable_error"
    assert dead["retry_count"] == 0


@pytest.mark.asyncio
async def test_3_9_task_timeout(e2e):
    """3.9 Task should dead-letter on executor timeout."""
    e2e.use_llm(slow_response(delay=999.0))

    e2e.ensure_agent()
    task_id = e2e.submit_task(task_timeout_seconds=120, input="timeout me")
    await e2e.db.set_task_timeout(task_id, 3)
    await e2e.start_worker("e2e-timeout")

    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=20.0)
    assert dead["dead_letter_reason"] == "task_timeout"


@pytest.mark.asyncio
async def test_3_10_max_steps_exceeded(e2e):
    """3.10 Infinite tool loop should hit max_steps and dead-letter."""
    e2e.use_llm(infinite_tool_loop())
    await e2e.start_worker("e2e-max-steps")

    e2e.ensure_agent()
    task_id = e2e.submit_task(max_steps=3, input="loop forever")
    dead = await e2e.wait_for_status(task_id, "dead_letter", timeout=25.0)
    assert dead["dead_letter_reason"] == "max_steps_exceeded"
