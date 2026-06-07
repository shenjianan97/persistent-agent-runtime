"""Planning Primitive — Task P5: ``GET /v1/tasks/{id}/plan`` shape (backend integration).

Drives the full stack through the isolated harness (real API service, real
Postgres checkpoints, in-process worker with the mocked-LLM provider — same
``e2e`` fixture every backend-integration scenario uses) and asserts P3's
read-only projection contract:

* **Populated:** an agent writes a known plan via a deterministic stub-LLM
  ``plan_write`` tool call; the endpoint returns
  ``{task_id, plan: [{id, title, status}, ...], updated_at}`` with items in
  written order and fields verbatim.

  ``plan_write`` is a BASE platform tool (product decision 2026-06-06,
  superseding the track's §A6 opt-in design): ``canonicalizeConfig`` seeds
  it into every agent via ``ValidationConstants.BASE_PLATFORM_TOOLS``, like
  web_search. ``test_plan_write_allowlist_survives_agent_creation`` pins
  that every created agent's stored config carries it.
* **Empty:** a task whose stub model never calls ``plan_write`` (the tool is
  allowlisted on every agent, but unused → no ``plan`` channel in the
  checkpoints) returns 200 ``{plan: []}`` — never a 404. (Also covers
  supervisor-style tasks that have checkpoints but no ``plan`` channel.)
* **No checkpoint yet:** a queued task (no worker started) returns
  200 ``{plan: []}`` with ``updated_at`` OMITTED from the JSON entirely
  (P3's ``@JsonInclude(NON_NULL)`` contract — clients type it optional).
* **404:** a nonexistent task id.
* **Read-only:** the path accepts GET only — mutation verbs are rejected
  (no ``PATCH``/``PUT``/``POST``/``DELETE`` mapping exists; the design's
  "plan mutation is Workflow's surface" decision).

Determinism: the stub LLM (``DynamicChatProvider`` patch of
``executor.providers.create_llm``) scripts the ``plan_write`` call — no
live model decides anything. Ports/DSNs come from the harness env
(``.tmp/e2e.env``); nothing is hardcoded, so the test is
worktree-concurrency-safe. Run via::

    make e2e-test PYTEST_ARGS='-k plan_read_api'
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolCall

from helpers.mock_llm import simple_response


# ---------------------------------------------------------------------------
# Fixture data + stub LLM script
# ---------------------------------------------------------------------------

#: The plan the stub agent writes — covers all three statuses so the
#: response-shape assertion exercises the full enum, and order matters.
PLAN_ITEMS: list[dict] = [
    {"id": "step-1", "title": "Collect the inputs", "status": "completed"},
    {"id": "step-2", "title": "Analyze the data", "status": "in_progress"},
    {"id": "step-3", "title": "Write the summary", "status": "pending"},
]

PLANNING_AGENT_CONFIG: dict = {
    "system_prompt": "You are a planning test agent.",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "temperature": 0.0,
    "allowed_tools": ["plan_write"],
}

#: "Planless" fixture: plan_write IS in this agent's stored config too (it is a
#: base platform tool seeded on every agent), but its scripted stub model never
#: calls it — so its tasks have no ``plan`` channel and exercise the empty shape.
NO_TOOLS_AGENT_CONFIG: dict = {
    "system_prompt": "You are a planless test agent.",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "temperature": 0.0,
    "allowed_tools": [],
}


def plan_write_then_final(
    items: list[dict], final_answer: str = "Plan recorded; work done."
) -> MagicMock:
    """Stub LLM: one ``plan_write`` tool call, then a final answer."""
    call_msg = AIMessage(
        content="",
        tool_calls=[
            ToolCall(name="plan_write", args={"items": items}, id="call_plan_1")
        ],
    )
    mock = MagicMock()
    mock.bind_tools.return_value = mock
    mock.ainvoke = AsyncMock(side_effect=[call_msg, AIMessage(content=final_answer)])
    return mock


# ---------------------------------------------------------------------------
# Populated shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_write_allowlist_survives_agent_creation(e2e):
    """Public-path verification: every agent created through ``POST
    /v1/agents`` carries ``plan_write`` in its stored config — it is a base
    platform tool seeded by canonicalization (product decision 2026-06-06,
    superseding the §A6 opt-in activation path)."""
    resp = e2e.ensure_agent(agent_config=PLANNING_AGENT_CONFIG)
    agent_id = resp["body"]["agent_id"]
    stored = e2e.api.get_agent(agent_id)["body"]["agent_config"]
    assert "plan_write" in stored.get("allowed_tools", []), (
        f"plan_write stripped by canonicalization: {stored.get('allowed_tools')}"
    )


@pytest.mark.asyncio
async def test_plan_populated_after_stub_agent_writes_plan(e2e):
    """Agent writes a known plan via ``plan_write`` → GET returns the full
    shape: ``task_id`` echo, items verbatim in written order, ``updated_at``
    present (a checkpoint exists). The real worker executes the real
    ``plan_write`` tool and the real Postgres checkpointer persists the
    ``plan`` channel the Java service projects."""
    e2e.use_llm(plan_write_then_final(PLAN_ITEMS))
    await e2e.start_worker("e2e-plan-populated-worker")

    # plan_write is a base platform tool — every agent gets it at creation.
    e2e.ensure_agent(agent_config=PLANNING_AGENT_CONFIG)
    task_id = e2e.submit_task(input="Plan the work, then do it.")
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    resp = e2e.api.get_task_plan(task_id)
    assert resp["status_code"] == 200
    body = resp["body"]

    assert body["task_id"] == task_id
    # Items verbatim: order preserved, exactly the {id, title, status}
    # fields P1 wrote — no extra keys, no normalization.
    assert body["plan"] == PLAN_ITEMS
    # A checkpoint exists for a completed task → updated_at is present
    # (ISO-8601 string per P3's OffsetDateTime serialization).
    assert body.get("updated_at"), "updated_at missing on a checkpointed task"


# ---------------------------------------------------------------------------
# Empty shape — task ran but never wrote a plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_empty_for_task_that_never_wrote_one(e2e):
    """A completed task whose stub model never calls ``plan_write`` (the base
    tool is allowlisted but unused, so its checkpoints have no ``plan``
    channel) → 200 ``{plan: []}``, not 404."""
    e2e.use_llm(simple_response("All done, no plan needed."))
    await e2e.start_worker("e2e-plan-empty-worker")

    e2e.ensure_agent(agent_config=NO_TOOLS_AGENT_CONFIG)
    task_id = e2e.submit_task(input="Just answer.")
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    resp = e2e.api.get_task_plan(task_id)
    assert resp["status_code"] == 200
    body = resp["body"]
    assert body["task_id"] == task_id
    assert body["plan"] == []


@pytest.mark.asyncio
async def test_plan_empty_with_updated_at_omitted_before_first_checkpoint(e2e):
    """A queued task (no worker started → no checkpoint rows) → 200
    ``{task_id, plan: []}`` and ``updated_at`` is OMITTED from the payload
    (``@JsonInclude(NON_NULL)``), not serialized as ``null``."""
    e2e.ensure_agent(agent_config=NO_TOOLS_AGENT_CONFIG)
    task_id = e2e.submit_task(input="Will never be picked up in this test.")

    resp = e2e.api.get_task_plan(task_id)
    assert resp["status_code"] == 200
    body = resp["body"]
    assert body["task_id"] == task_id
    assert body["plan"] == []
    assert "updated_at" not in body, (
        "updated_at must be omitted (not null) when no checkpoint exists"
    )


# ---------------------------------------------------------------------------
# 404 — missing task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_404_for_nonexistent_task(e2e):
    missing_id = str(uuid.uuid4())
    resp = e2e.api.get_task_plan(
        missing_id, expected_status=404, raise_for_status=False
    )
    assert resp["status_code"] == 404


# ---------------------------------------------------------------------------
# Read-only: no mutation verb is mapped on the plan path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_endpoint_rejects_mutation_verbs(e2e):
    """The plan is agent-owned scratchpad state — the API surface is GET
    only (design decision: plan mutation is the Workflow resource's
    surface, not this endpoint's).

    Observed contract: Spring raises
    ``HttpRequestMethodNotSupportedException`` (no non-GET mapping exists),
    which this repo's ``GlobalExceptionHandler`` currently surfaces as a
    **500** rather than the conventional 405 (pre-existing, API-wide
    behavior for every endpoint — verified in ``.tmp/e2e-api-service.log``;
    minor hygiene finding, not a P3 defect). The load-bearing assertions:
    every mutation verb is rejected (non-2xx), and the resource is
    unchanged afterwards."""
    e2e.ensure_agent(agent_config=NO_TOOLS_AGENT_CONFIG)
    task_id = e2e.submit_task(input="Read-only probe target.")

    for method in ("PUT", "PATCH", "POST", "DELETE"):
        payload = (
            {"plan": [{"id": "x", "title": "nope", "status": "pending"}]}
            if method != "DELETE"
            else None
        )
        resp = e2e.api._request(
            method,
            f"/tasks/{task_id}/plan",
            payload=payload,
            raise_for_status=False,
        )
        assert resp["status_code"] in (404, 405, 500), (
            f"{method} /tasks/{{id}}/plan must be rejected (unmapped verb); "
            f"got {resp['status_code']}"
        )

    # No mutation happened: the plan is still readable and still empty.
    after = e2e.api.get_task_plan(task_id)
    assert after["status_code"] == 200
    assert after["body"]["plan"] == []
