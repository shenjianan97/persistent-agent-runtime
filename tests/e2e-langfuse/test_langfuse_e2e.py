"""
Langfuse E2E tests.

Requires a running Langfuse instance (make test-langfuse-up) AND
the full platform stack (make start).

Run with:
    make test-e2e-langfuse
"""

import os
import time

import pytest

from conftest import LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, langfuse_request
from helpers.mock_llm import callback_friendly_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_endpoint(e2e, *, name: str = "e2e-langfuse", host: str | None = None,
                     public_key: str | None = None, secret_key: str | None = None) -> dict:
    """Create a Langfuse endpoint via the platform API and return the response body."""
    return e2e.api._request(
        "POST",
        "/langfuse-endpoints",
        payload={
            "name": name,
            "host": host or LANGFUSE_HOST,
            "public_key": public_key or LANGFUSE_PUBLIC_KEY,
            "secret_key": secret_key or LANGFUSE_SECRET_KEY,
        },
        expected_status=201,
    )["body"]


def _poll_langfuse_traces(
    *,
    task_id: str | None = None,
    public_key: str = LANGFUSE_PUBLIC_KEY,
    secret_key: str = LANGFUSE_SECRET_KEY,
    timeout: float = 30.0,
    interval: float = 1.0,
) -> list[dict]:
    """Poll Langfuse until at least one trace matching task_id appears (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = langfuse_request(
            "GET",
            "/api/public/traces",
            public_key=public_key,
            secret_key=secret_key,
            raise_for_status=False,
        )
        if resp["status_code"] == 200:
            traces = resp["body"].get("data", [])
            if task_id:
                matching = [t for t in traces if _trace_matches_task(t, task_id)]
                if matching:
                    return matching
            elif traces:
                return traces
        time.sleep(interval)
    return []


def _trace_matches_task(trace: dict, task_id: str) -> bool:
    """Return True if a Langfuse trace is associated with the given task_id."""
    # The worker tags traces with task_id in the name or metadata
    name: str = trace.get("name", "")
    metadata: dict = trace.get("metadata") or {}
    tags: list = trace.get("tags") or []
    return (
        task_id in name
        or metadata.get("task_id") == task_id
        or task_id in tags
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connectivity_endpoint(e2e):
    """Create a Langfuse endpoint then call the /test endpoint — expect reachable: true."""
    endpoint = _create_endpoint(e2e, name="e2e-connectivity-test")
    endpoint_id = endpoint["endpoint_id"]

    result = e2e.api._request(
        "POST",
        f"/langfuse-endpoints/{endpoint_id}/test",
        expected_status=200,
    )["body"]

    assert result.get("reachable") is True, f"Expected reachable=true, got: {result}"
    assert result.get("message", "").upper() in {"OK", ""} or result.get("message"), \
        f"Unexpected message: {result}"


@pytest.mark.asyncio
async def test_traces_published_to_langfuse(e2e):
    """Core E2E: submit a task with langfuse_endpoint_id and verify a trace lands in Langfuse."""
    e2e.use_llm(callback_friendly_response("E2E trace verification"))

    endpoint = _create_endpoint(e2e, name="e2e-trace-test")
    endpoint_id = endpoint["endpoint_id"]

    await e2e.start_worker("e2e-trace-worker")

    task_id = e2e.submit_task(
        allowed_tools=[],
        input="Say hello for trace verification",
        langfuse_endpoint_id=endpoint_id,
    )

    completed = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert completed["status"] == "completed"

    # Allow a short buffer for Langfuse to ingest the trace asynchronously
    traces = _poll_langfuse_traces(task_id=task_id, timeout=30.0)

    assert traces, (
        f"Expected at least one Langfuse trace for task_id={task_id}, but none found. "
        "Check that the worker is publishing traces and Langfuse is running."
    )

    # Verify the trace has useful metadata
    trace = traces[0]
    assert trace.get("id"), "Trace should have an id"


@pytest.mark.asyncio
async def test_task_without_langfuse_completes_with_cost(e2e):
    """Regression: a task submitted without langfuse_endpoint_id still completes with cost data."""
    e2e.use_llm(callback_friendly_response("No Langfuse needed"))

    await e2e.start_worker("e2e-no-langfuse-worker")

    task_id = e2e.submit_task(
        allowed_tools=[],
        input="Hello without observability",
        # No langfuse_endpoint_id
    )

    completed = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert completed["status"] == "completed"
    assert completed["total_cost_microdollars"] >= 0, \
        "Cost tracking should work independently of Langfuse"

    # Verify checkpoint-based observability data is still populated
    async def load_observability():
        response = e2e.api.get_observability(task_id)["body"]
        if response.get("items") and len(response["items"]) > 0:
            return response
        return None

    observability = await e2e.wait_for(
        load_observability,
        timeout=20.0,
        description="Checkpoint-based observability data (no Langfuse)",
    )
    assert observability["items"], "Checkpoint observability items should be present"


@pytest.mark.asyncio
async def test_bad_credentials_task_still_completes(e2e):
    """Graceful degradation: bad Langfuse credentials should not cause task failure."""
    e2e.use_llm(callback_friendly_response("Degraded observability response"))

    # Create endpoint with deliberately wrong secret key
    endpoint = _create_endpoint(
        e2e,
        name="e2e-bad-creds",
        secret_key="sk-lf-WRONG-KEY",
    )
    endpoint_id = endpoint["endpoint_id"]

    await e2e.start_worker("e2e-bad-creds-worker")

    task_id = e2e.submit_task(
        allowed_tools=[],
        input="Test graceful degradation",
        langfuse_endpoint_id=endpoint_id,
    )

    # Task must still complete even though Langfuse auth will fail
    completed = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert completed["status"] == "completed", \
        "Task should complete successfully despite bad Langfuse credentials"

    # Platform checkpoint cost data is populated regardless
    assert completed["total_cost_microdollars"] >= 0, \
        "Cost data should still be tracked even when Langfuse auth fails"


@pytest.mark.asyncio
async def test_crud_endpoints(e2e):
    """Full CRUD lifecycle for Langfuse endpoint management."""
    api = e2e.api

    # ── POST create ────────────────────────────────────────────────────────
    create_body = api._request(
        "POST",
        "/langfuse-endpoints",
        payload={
            "name": "crud-test-endpoint",
            "host": LANGFUSE_HOST,
            "public_key": LANGFUSE_PUBLIC_KEY,
            "secret_key": LANGFUSE_SECRET_KEY,
        },
        expected_status=201,
    )["body"]
    endpoint_id = create_body["endpoint_id"]
    assert endpoint_id, "Create should return an endpoint_id"

    # ── GET list ───────────────────────────────────────────────────────────
    list_body = api._request("GET", "/langfuse-endpoints", expected_status=200)["body"]
    endpoints = list_body if isinstance(list_body, list) else list_body.get("endpoints", [])
    ids = [ep["endpoint_id"] for ep in endpoints]
    assert endpoint_id in ids, f"New endpoint {endpoint_id} should appear in list"

    # ── GET by id ──────────────────────────────────────────────────────────
    get_body = api._request(
        "GET", f"/langfuse-endpoints/{endpoint_id}", expected_status=200
    )["body"]
    assert get_body["endpoint_id"] == endpoint_id
    assert get_body["name"] == "crud-test-endpoint"

    # ── PUT update name ────────────────────────────────────────────────────
    api._request(
        "PUT",
        f"/langfuse-endpoints/{endpoint_id}",
        payload={"name": "crud-test-endpoint-updated"},
        expected_status=200,
    )

    # ── GET by id after update ─────────────────────────────────────────────
    updated_body = api._request(
        "GET", f"/langfuse-endpoints/{endpoint_id}", expected_status=200
    )["body"]
    assert updated_body["name"] == "crud-test-endpoint-updated", \
        "Name should be updated after PUT"

    # ── DELETE ─────────────────────────────────────────────────────────────
    api._request("DELETE", f"/langfuse-endpoints/{endpoint_id}", expected_status=204)

    # ── GET by id after delete → 404 ──────────────────────────────────────
    not_found = api._request(
        "GET",
        f"/langfuse-endpoints/{endpoint_id}",
        expected_status=404,
        raise_for_status=False,
    )
    assert not_found["status_code"] == 404, \
        f"Endpoint should return 404 after deletion, got {not_found['status_code']}"
