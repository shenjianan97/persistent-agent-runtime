import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status_code: int, body: dict[str, Any]):
        super().__init__(f"API request failed with status={status_code}: {body}")
        self.status_code = status_code
        self.body = body


class ApiClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.request_timeout = float(os.getenv("E2E_API_REQUEST_TIMEOUT_SECONDS", "10.0"))

    def create_agent(self, display_name="E2E Test Agent",
                     agent_config=None, expected_status=201, raise_for_status=True,
                     **overrides):
        """Create an agent. Returns response dict with auto-generated agent_id."""
        config = agent_config or {
            "system_prompt": "You are a test assistant.",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 0.5,
            "allowed_tools": ["calculator"]
        }
        payload = {
            "display_name": display_name,
            "agent_config": config,
            **overrides
        }
        return self._request("POST", "/agents", payload, expected_status, raise_for_status)

    def get_agent(self, agent_id):
        """Get agent detail. Returns response dict."""
        return self._request("GET", f"/agents/{agent_id}")

    def list_agents(self, status=None, limit=None):
        """List agents. Returns list."""
        params = {}
        if status: params["status"] = status
        if limit: params["limit"] = str(limit)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        path = f"/agents{'?' + query if query else ''}"
        return self._request("GET", path)

    def update_agent(self, agent_id, display_name, agent_config, status):
        """Update agent. Returns response dict."""
        payload = {
            "display_name": display_name,
            "agent_config": agent_config,
            "status": status
        }
        return self._request("PUT", f"/agents/{agent_id}", payload)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        req = urllib.request.Request(url, method=method, headers=headers, data=data)
        expected = (expected_status,) if isinstance(expected_status, int) else expected_status

        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                raw = resp.read().decode("utf-8")
                body = json.loads(raw) if raw else None
                result = {"status_code": resp.status, "body": body}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"message": raw}
            result = {"status_code": exc.code, "body": body}
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(
                f"API request to {url} timed out after {self.request_timeout}s: {exc}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Unable to reach API at {url}: {exc}") from exc

        if raise_for_status and result["status_code"] not in expected:
            raise ApiError(result["status_code"], result["body"])
        return result

    # Legacy keys that are no longer part of the task submission contract.
    # Tests that need specific agent configs must create a dedicated agent instead.
    _LEGACY_AGENT_CONFIG_KEYS = {"agent_config", "system_prompt", "provider", "model", "temperature", "allowed_tools"}

    def submit_task(self, *, expected_status: int | tuple[int, ...] = 201, raise_for_status: bool = True, **overrides: Any) -> dict[str, Any]:
        """Submit a task referencing an existing agent."""
        # Fail fast if callers pass legacy inline-config keys
        legacy = self._LEGACY_AGENT_CONFIG_KEYS & overrides.keys()
        if legacy:
            raise TypeError(
                f"submit_task() received legacy agent_config keys {legacy}. "
                "Inline agent config is no longer supported. "
                "Create a dedicated agent with the required config and pass its agent_id instead."
            )
        payload = {
            "agent_id": overrides.pop("agent_id", None),
            "input": overrides.pop("input", "What is 2+2?"),
            "max_retries": overrides.pop("max_retries", 3),
            "max_steps": overrides.pop("max_steps", 10),
            "task_timeout_seconds": overrides.pop("task_timeout_seconds", 120),
        }
        if "langfuse_endpoint_id" in overrides:
            payload["langfuse_endpoint_id"] = overrides.pop("langfuse_endpoint_id")
        if "tenant_id" in overrides:
            payload["tenant_id"] = overrides.pop("tenant_id")
        return self._request("POST", "/tasks", payload, expected_status, raise_for_status)

    def get_task(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}", expected_status=expected_status, raise_for_status=raise_for_status)

    def get_checkpoints(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}/checkpoints", expected_status=expected_status, raise_for_status=raise_for_status)

    def get_observability(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}/observability", expected_status=expected_status, raise_for_status=raise_for_status)

    def cancel_task(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("POST", f"/tasks/{task_id}/cancel", expected_status=expected_status, raise_for_status=raise_for_status)

    def redrive_task(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("POST", f"/tasks/{task_id}/redrive", expected_status=expected_status, raise_for_status=raise_for_status)

    def dev_expire_lease(
        self,
        task_id: str,
        *,
        lease_owner: str | None = None,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        payload = {"lease_owner": lease_owner} if lease_owner is not None else None
        return self._request("POST", f"/dev/tasks/{task_id}/expire-lease", payload, expected_status, raise_for_status)

    def dev_force_dead_letter(
        self,
        task_id: str,
        *,
        reason: str = "non_retryable_error",
        error_code: str | None = None,
        error_message: str | None = None,
        last_worker_id: str | None = None,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": reason}
        if error_code is not None:
            payload["error_code"] = error_code
        if error_message is not None:
            payload["error_message"] = error_message
        if last_worker_id is not None:
            payload["last_worker_id"] = last_worker_id
        return self._request("POST", f"/dev/tasks/{task_id}/force-dead-letter", payload, expected_status, raise_for_status)

    def get_dead_letters(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 50,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        params = {"limit": str(limit)}
        if agent_id:
            params["agent_id"] = agent_id
        query = urllib.parse.urlencode(params)
        return self._request("GET", f"/tasks/dead-letter?{query}", expected_status=expected_status, raise_for_status=raise_for_status)

    def health(self, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("GET", "/health", expected_status=expected_status, raise_for_status=raise_for_status)

    # --- HITL endpoints ---

    def approve_task(
        self,
        task_id: str,
        *,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/approve"""
        return self._request("POST", f"/tasks/{task_id}/approve", expected_status=expected_status, raise_for_status=raise_for_status)

    def reject_task(
        self,
        task_id: str,
        reason: str,
        *,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/reject"""
        return self._request("POST", f"/tasks/{task_id}/reject", {"reason": reason}, expected_status=expected_status, raise_for_status=raise_for_status)

    def respond_to_task(
        self,
        task_id: str,
        message: str,
        *,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/respond"""
        return self._request("POST", f"/tasks/{task_id}/respond", {"message": message}, expected_status=expected_status, raise_for_status=raise_for_status)

    # --- Raw (non-raising) variants for status-code assertion tests ---

    def approve_task_raw(self, task_id: str) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/approve — returns status/body without raising."""
        return self._request("POST", f"/tasks/{task_id}/approve", raise_for_status=False)

    def reject_task_raw(self, task_id: str, reason: str) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/reject — returns status/body without raising."""
        return self._request("POST", f"/tasks/{task_id}/reject", {"reason": reason}, raise_for_status=False)

    def respond_to_task_raw(self, task_id: str, message: str) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/respond — returns status/body without raising."""
        return self._request("POST", f"/tasks/{task_id}/respond", {"message": message}, raise_for_status=False)

    # --- Task events ---

    def get_task_events(
        self,
        task_id: str,
        *,
        limit: int = 100,
        expected_status: int | tuple[int, ...] = 200,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        """GET /v1/tasks/{task_id}/events"""
        return self._request("GET", f"/tasks/{task_id}/events?limit={limit}", expected_status=expected_status, raise_for_status=raise_for_status)

    def poll_until(
        self,
        task_id: str,
        target_status: str,
        *,
        timeout: float = 20.0,
        interval: float = 0.25,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            result = self.get_task(task_id)
            body = result["body"]
            last = body
            status = body["status"]
            if status == target_status:
                return body
            if status in {"completed", "dead_letter"} and status != target_status:
                return body
            time.sleep(interval)
        raise TimeoutError(
            f"Task {task_id} did not reach {target_status} in {timeout}s. Last={None if last is None else last.get('status')}"
        )

    def poll_for_statuses(
        self,
        task_id: str,
        statuses: set[str],
        *,
        timeout: float = 20.0,
        interval: float = 0.25,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            body = self.get_task(task_id)["body"]
            last = body
            if body["status"] in statuses:
                return body
            time.sleep(interval)
        raise TimeoutError(
            f"Task {task_id} did not reach one of {statuses} in {timeout}s. Last={None if last is None else last.get('status')}"
        )
