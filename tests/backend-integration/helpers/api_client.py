import json
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
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                result = {"status_code": resp.status, "body": body}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"message": raw}
            result = {"status_code": exc.code, "body": body}
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Unable to reach API at {url}: {exc}") from exc

        if raise_for_status and result["status_code"] not in expected:
            raise ApiError(result["status_code"], result["body"])
        return result

    def submit_task(self, *, expected_status: int | tuple[int, ...] = 201, raise_for_status: bool = True, **overrides: Any) -> dict[str, Any]:
        payload = {
            "agent_id": overrides.get("agent_id", "e2e_agent"),
            "agent_config": {
                "system_prompt": overrides.get("system_prompt", "You are a test assistant."),
                "model": overrides.get("model", "claude-sonnet-4-6"),
                "temperature": overrides.get("temperature", 0.5),
                "allowed_tools": overrides.get("allowed_tools", ["calculator"]),
            },
            "input": overrides.get("input", "What is 2+2?"),
            "max_retries": overrides.get("max_retries", 3),
            "max_steps": overrides.get("max_steps", 10),
            "task_timeout_seconds": overrides.get("task_timeout_seconds", 120),
        }
        if "tenant_id" in overrides:
            payload["tenant_id"] = overrides["tenant_id"]
        return self._request("POST", "/tasks", payload, expected_status, raise_for_status)

    def get_task(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}", expected_status=expected_status, raise_for_status=raise_for_status)

    def get_checkpoints(self, task_id: str, *, expected_status: int | tuple[int, ...] = 200, raise_for_status: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}/checkpoints", expected_status=expected_status, raise_for_status=raise_for_status)

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
