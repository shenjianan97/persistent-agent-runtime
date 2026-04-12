"""Integration tests for Track 1: Output Artifact Storage.

Tests the end-to-end artifact flow:
1. Agent calls upload_artifact tool during task execution
2. Artifact metadata appears in list endpoint
3. Artifact file can be downloaded with correct content
"""

import pytest

from helpers.mock_llm import upload_artifact_call, simple_response


@pytest.mark.asyncio
async def test_upload_and_list_artifact(e2e):
    """Agent uploads an artifact via upload_artifact tool.
    Verify it appears in the artifact list endpoint."""

    artifact_content = "# Analysis Report\n\nThis is a test report.\n"
    artifact_filename = "report.md"

    e2e.use_llm(upload_artifact_call(
        filename=artifact_filename,
        content=artifact_content,
        content_type="text/markdown",
        final_answer="Report has been saved as report.md.",
    ))
    await e2e.start_worker("e2e-artifact-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant that produces reports.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["upload_artifact"],
    })

    task_id = e2e.submit_task(input="Write a short analysis report.")

    completed = await e2e.wait_for_status(task_id, "completed", timeout=30.0)
    assert "report" in str(completed["output"]["result"]).lower()

    # List artifacts
    artifacts_response = e2e.api.list_artifacts(task_id)
    artifacts = artifacts_response["body"]
    assert len(artifacts) == 1

    artifact = artifacts[0]
    assert artifact["filename"] == artifact_filename
    assert artifact["direction"] == "output"
    assert artifact["contentType"] == "text/markdown"
    assert artifact["sizeBytes"] == len(artifact_content.encode("utf-8"))


@pytest.mark.asyncio
async def test_download_artifact_content(e2e):
    """After an artifact is uploaded, download it and verify the content matches."""

    artifact_content = "col1,col2,col3\n1,2,3\n4,5,6\n"
    artifact_filename = "data.csv"

    e2e.use_llm(upload_artifact_call(
        filename=artifact_filename,
        content=artifact_content,
        content_type="text/csv",
    ))
    await e2e.start_worker("e2e-artifact-dl-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["upload_artifact"],
    })

    task_id = e2e.submit_task(input="Generate a CSV file.")
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    # Download artifact
    download = e2e.api.download_artifact(task_id, artifact_filename)
    assert download["status_code"] == 200
    assert download["body"].decode("utf-8") == artifact_content
    assert "text/csv" in download["content_type"]


@pytest.mark.asyncio
async def test_list_artifacts_empty(e2e):
    """List artifacts for a task with no artifacts returns empty list."""

    e2e.use_llm(simple_response("Done, no artifacts."))
    await e2e.start_worker("e2e-artifact-empty-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": [],
    })

    task_id = e2e.submit_task(input="Just respond.")
    await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    # List artifacts — should be empty
    artifacts_response = e2e.api.list_artifacts(task_id)
    artifacts = artifacts_response["body"]
    assert artifacts == []


@pytest.mark.asyncio
async def test_download_nonexistent_artifact_returns_404(e2e):
    """Download a non-existent artifact returns 404."""

    e2e.use_llm(simple_response("Done."))
    await e2e.start_worker("e2e-artifact-404-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": [],
    })

    task_id = e2e.submit_task(input="Just respond.")
    await e2e.wait_for_status(task_id, "completed", timeout=20.0)

    # Download non-existent artifact — should be 404
    download = e2e.api.download_artifact(
        task_id, "nonexistent.pdf",
        expected_status=404, raise_for_status=False,
    )
    assert download["status_code"] == 404


@pytest.mark.asyncio
async def test_list_artifacts_with_direction_filter(e2e):
    """List artifacts with direction filter returns only matching artifacts."""

    artifact_content = "Test content"

    e2e.use_llm(upload_artifact_call(
        filename="filtered.txt",
        content=artifact_content,
        content_type="text/plain",
    ))
    await e2e.start_worker("e2e-artifact-filter-worker")

    e2e.ensure_agent(agent_config={
        "system_prompt": "You are a test assistant.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "allowed_tools": ["upload_artifact"],
    })

    task_id = e2e.submit_task(input="Create a text file.")
    await e2e.wait_for_status(task_id, "completed", timeout=30.0)

    # Filter by output — should return the artifact
    output_artifacts = e2e.api.list_artifacts(task_id, direction="output")
    assert len(output_artifacts["body"]) == 1

    # Filter by input — should return empty (no input artifacts in Track 1)
    input_artifacts = e2e.api.list_artifacts(task_id, direction="input")
    assert len(input_artifacts["body"]) == 0
