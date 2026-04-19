"""Backend-integration tests for context_management field validation (Track 7 AC 12).

AC 12: POST/PUT /v1/agents validates context_management fields.
  - Valid context_management sub-object → 201/200
  - Absent context_management → 201 (absence always valid)
  - summarizer_model pointing at inactive/wrong-provider model → 400
  - exclude_tools with > 50 entries → 400
  - Unknown field (e.g. "enabled": true) inside context_management → 400
    (Spring Boot Jackson FAIL_ON_UNKNOWN_PROPERTIES=true enforces this)
  - pre_tier3_memory_flush=true with memory.enabled=false → 201 (cross-field
    check is the worker's responsibility; API does not reject this combination)

Design doc: docs/design-docs/phase-2/track-7-context-window-management.md
§Agent config extension — "Validation and consistency rules".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from helpers.api_client import ApiClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_CONFIG = {
    "system_prompt": "You are a test assistant.",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "temperature": 0.5,
    "allowed_tools": ["web_search"],
}


def _config_with_cm(cm: dict | None = None, memory: dict | None = None) -> dict:
    """Build an agent_config that includes context_management and optionally memory."""
    config = {**BASE_CONFIG}
    if cm is not None:
        config["context_management"] = cm
    if memory is not None:
        config["memory"] = memory
    return config


def _raw_post_agent(base_url: str, payload: dict, timeout: float = 10.0) -> int:
    """POST /agents with a raw JSON payload, return HTTP status code."""
    req = urllib.request.Request(
        f"{base_url}/agents",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _raw_put_agent(base_url: str, agent_id: str, payload: dict, timeout: float = 10.0) -> int:
    """PUT /agents/{id} with a raw JSON payload, return HTTP status code."""
    req = urllib.request.Request(
        f"{base_url}/agents/{agent_id}",
        method="PUT",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _base_put_payload(display_name: str, agent_config: dict) -> dict:
    """Build a minimal PUT /agents/{id} payload."""
    return {
        "display_name": display_name,
        "agent_config": agent_config,
        "status": "active",
        "max_concurrent_tasks": 5,
        "budget_max_per_task": 500000,
        "budget_max_per_hour": 5000000,
    }


# ---------------------------------------------------------------------------
# AC 12 — POST /v1/agents validation
# ---------------------------------------------------------------------------


class TestContextManagementValidationOnCreate:
    """context_management validation on POST /v1/agents."""

    def test_absent_context_management_accepted(self, api_client: ApiClient) -> None:
        """No context_management sub-object → 201 (absence is always valid)."""
        resp = api_client.create_agent(
            display_name="CM absent",
            agent_config=BASE_CONFIG,
            expected_status=201,
        )
        assert resp["status_code"] == 201

    def test_empty_context_management_accepted(self, api_client: ApiClient) -> None:
        """Empty context_management sub-object {} → 201 (all fields nullable/optional)."""
        resp = api_client.create_agent(
            display_name="CM empty",
            agent_config=_config_with_cm(cm={}),
            expected_status=201,
        )
        assert resp["status_code"] == 201

    def test_valid_pre_tier3_memory_flush_accepted(self, api_client: ApiClient) -> None:
        """pre_tier3_memory_flush=true with valid agent config → 201."""
        resp = api_client.create_agent(
            display_name="CM flush true",
            agent_config=_config_with_cm(cm={"pre_tier3_memory_flush": True}),
            expected_status=201,
        )
        assert resp["status_code"] == 201

    def test_valid_exclude_tools_50_entries_accepted(self, api_client: ApiClient) -> None:
        """exclude_tools with exactly 50 entries → 201 (boundary is inclusive at 50)."""
        fifty_tools = [f"custom_tool_{i}" for i in range(50)]
        resp = api_client.create_agent(
            display_name="CM 50 exclude tools",
            agent_config=_config_with_cm(cm={"exclude_tools": fifty_tools}),
            expected_status=201,
        )
        assert resp["status_code"] == 201

    def test_exclude_tools_51_entries_rejected(self, api_client: ApiClient) -> None:
        """exclude_tools with 51 entries → 400 (max is 50)."""
        fifty_one_tools = [f"custom_tool_{i}" for i in range(51)]
        resp = api_client.create_agent(
            display_name="CM 51 exclude tools",
            agent_config=_config_with_cm(cm={"exclude_tools": fifty_one_tools}),
            expected_status=(400, 201),
            raise_for_status=False,
        )
        assert resp["status_code"] == 400, (
            f"Expected 400 for 51 exclude_tools entries, got {resp['status_code']}: {resp['body']}"
        )

    def test_unknown_summarizer_model_rejected(self, api_client: ApiClient) -> None:
        """summarizer_model pointing at non-existent model → 400."""
        resp = api_client.create_agent(
            display_name="CM bad summarizer",
            agent_config=_config_with_cm(
                cm={"summarizer_model": "does-not-exist-model-xyz"}
            ),
            expected_status=(400, 201),
            raise_for_status=False,
        )
        assert resp["status_code"] == 400, (
            f"Expected 400 for unknown summarizer_model, got {resp['status_code']}: {resp['body']}"
        )

    def test_unknown_field_enabled_inside_cm_rejected(self, api_client: ApiClient) -> None:
        """context_management.enabled=true → 400.

        ContextManagementConfigRequest has no 'enabled' field. Jackson's
        FAIL_ON_UNKNOWN_PROPERTIES=true enforces rejection at the deserialization
        layer — no manual guard needed.
        """
        raw_config = {
            **BASE_CONFIG,
            "context_management": {
                "enabled": True,  # unknown field — Jackson must reject this
            },
        }
        status = _raw_post_agent(
            api_client.base,
            {"display_name": "CM enabled field", "agent_config": raw_config},
        )
        assert status == 400, (
            f"Expected 400 for context_management.enabled (unknown field), got {status}"
        )

    def test_pre_tier3_flush_true_memory_disabled_accepted(
        self, api_client: ApiClient
    ) -> None:
        """pre_tier3_memory_flush=true AND memory.enabled=false → 201.

        The API does NOT cross-validate this combination. Runtime gating
        (checking memory.enabled before firing the flush) is the worker's job
        (AC 13, Task 9 / pipeline.py). The API accepts the payload verbatim.
        """
        resp = api_client.create_agent(
            display_name="CM flush+memory disabled",
            agent_config=_config_with_cm(
                cm={"pre_tier3_memory_flush": True},
                memory={"enabled": False},
            ),
            expected_status=201,
        )
        assert resp["status_code"] == 201

    def test_valid_full_context_management_accepted(self, api_client: ApiClient) -> None:
        """All valid context_management fields together → 201."""
        resp = api_client.create_agent(
            display_name="CM full valid",
            agent_config=_config_with_cm(
                cm={
                    "exclude_tools": ["my_custom_tool"],
                    "pre_tier3_memory_flush": False,
                    # summarizer_model omitted — would need a known active model
                }
            ),
            expected_status=201,
        )
        assert resp["status_code"] == 201


# ---------------------------------------------------------------------------
# AC 12 — PUT /v1/agents/{id} validation (same rules as POST)
# ---------------------------------------------------------------------------


class TestContextManagementValidationOnUpdate:
    """context_management validation on PUT /v1/agents/{id}."""

    def test_put_empty_context_management_accepted(self, api_client: ApiClient) -> None:
        """PUT with empty context_management sub-object → 200."""
        create_resp = api_client.create_agent(
            display_name="PUT CM empty",
            agent_config=BASE_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        update_resp = api_client.update_agent(
            agent_id,
            agent_config=_config_with_cm(cm={}),
        )
        assert update_resp["status_code"] == 200

    def test_put_pre_tier3_flush_accepted(self, api_client: ApiClient) -> None:
        """PUT with pre_tier3_memory_flush=true → 200."""
        create_resp = api_client.create_agent(
            display_name="PUT CM flush",
            agent_config=BASE_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        update_resp = api_client.update_agent(
            agent_id,
            agent_config=_config_with_cm(cm={"pre_tier3_memory_flush": True}),
        )
        assert update_resp["status_code"] == 200

    def test_put_exclude_tools_51_entries_rejected(self, api_client: ApiClient) -> None:
        """PUT with 51 exclude_tools entries → 400."""
        create_resp = api_client.create_agent(
            display_name="PUT 51 tools",
            agent_config=BASE_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        fifty_one_tools = [f"custom_tool_{i}" for i in range(51)]
        status = _raw_put_agent(
            api_client.base,
            agent_id,
            _base_put_payload(
                "PUT 51 tools",
                _config_with_cm(cm={"exclude_tools": fifty_one_tools}),
            ),
        )
        assert status == 400, (
            f"Expected 400 for 51 exclude_tools entries on PUT, got {status}"
        )

    def test_put_unknown_summarizer_model_rejected(self, api_client: ApiClient) -> None:
        """PUT with unknown summarizer_model → 400."""
        create_resp = api_client.create_agent(
            display_name="PUT bad summarizer",
            agent_config=BASE_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        status = _raw_put_agent(
            api_client.base,
            agent_id,
            _base_put_payload(
                "PUT bad summarizer",
                _config_with_cm(cm={"summarizer_model": "nonexistent-model-xyz"}),
            ),
        )
        assert status == 400, (
            f"Expected 400 for unknown summarizer_model on PUT, got {status}"
        )

    def test_put_unknown_field_enabled_in_cm_rejected(self, api_client: ApiClient) -> None:
        """PUT with context_management.enabled=true → 400 (Jackson unknown-field guard)."""
        create_resp = api_client.create_agent(
            display_name="PUT CM enabled",
            agent_config=BASE_CONFIG,
        )
        agent_id = create_resp["body"]["agent_id"]

        raw_config = {
            **BASE_CONFIG,
            "context_management": {"enabled": True},
        }
        status = _raw_put_agent(
            api_client.base,
            agent_id,
            _base_put_payload("PUT CM enabled", raw_config),
        )
        assert status == 400, (
            f"Expected 400 for context_management.enabled on PUT, got {status}"
        )
