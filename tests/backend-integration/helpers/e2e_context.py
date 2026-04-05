"""Shared scenario context for end-to-end tests.

This wrapper keeps test cases focused on behavior rather than plumbing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from helpers.api_client import ApiClient, ApiError
from helpers.db import DbHelper
from helpers.mock_llm import DynamicChatProvider
from helpers.waiting import wait_for_async, wait_until_task_status_async, wait_until_task_statuses_async


@dataclass
class E2EContext:
    """Small facade that centralizes common E2E test actions."""

    api: ApiClient
    db: DbHelper
    llm: DynamicChatProvider
    workers: Any

    _default_agent_id: str | None = None

    def ensure_agent(self, **kwargs):
        """Create an agent and cache its ID for subsequent submit_task calls.

        Accepts an optional `agent_id` kwarg for test readability — it is
        stripped from the API payload (agent_id is server-generated) and used
        as display_name if none is provided.
        """
        friendly_id = kwargs.pop("agent_id", None)
        if friendly_id and "display_name" not in kwargs:
            kwargs["display_name"] = friendly_id
        resp = self.api.create_agent(**kwargs)
        agent_id = resp["body"]["agent_id"]
        self._default_agent_id = agent_id
        return resp

    def use_llm(self, llm_mock: Any) -> None:
        self.llm.set_llm(llm_mock)

    def use_llm_factory(self, factory: Callable[[], Any]) -> None:
        self.llm.set_factory(factory)

    async def start_worker(self, worker_id: str, **kwargs: Any) -> Any:
        return await self.workers.start(worker_id=worker_id, **kwargs)

    async def stop_worker(self, worker: Any) -> None:
        await self.workers.stop(worker)

    async def stop_workers(self) -> None:
        await self.workers.stop_all()

    def submit_task(self, **overrides: Any) -> str:
        if "agent_id" not in overrides and self._default_agent_id:
            overrides["agent_id"] = self._default_agent_id
        return self.api.submit_task(**overrides)["body"]["task_id"]

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.api.get_task(task_id)["body"]

    def resume_task(self, task_id: str) -> dict:
        return self.api.resume_task(task_id)["body"]

    def get_events(self, task_id: str) -> list:
        resp = self.api.get_task_events(task_id)
        return resp["body"]["events"]

    def get_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        return self.api.get_checkpoints(task_id)["body"]["checkpoints"]

    def dev_expire_lease(self, task_id: str, **overrides: Any) -> dict[str, Any]:
        return self.api.dev_expire_lease(task_id, **overrides)["body"]

    def dev_force_dead_letter(self, task_id: str, **overrides: Any) -> dict[str, Any]:
        return self.api.dev_force_dead_letter(task_id, **overrides)["body"]

    async def wait_for_status(self, task_id: str, status: str, timeout: float = 20.0) -> dict[str, Any]:
        return await wait_until_task_status_async(self.api, task_id, status, timeout=timeout)

    async def wait_for_statuses(self, task_id: str, statuses: set[str], timeout: float = 20.0) -> dict[str, Any]:
        return await wait_until_task_statuses_async(self.api, task_id, statuses, timeout=timeout)

    async def wait_for(self, check: Callable[[], Any], timeout: float, description: str, interval: float = 0.2) -> Any:
        return await wait_for_async(check, timeout=timeout, interval=interval, description=description)

    @staticmethod
    def parse_json_array(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            return json.loads(value)
        if isinstance(value, list):
            return value
        return []
