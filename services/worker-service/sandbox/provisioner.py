"""E2B sandbox provisioner — lifecycle management for code execution environments."""

import asyncio
import logging
import os
import time

from e2b_code_interpreter import Sandbox

logger = logging.getLogger(__name__)

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = [1, 2, 4]


class SandboxProvisionError(Exception):
    """Raised when sandbox provisioning fails after all retries."""

    def __init__(self, template: str, message: str):
        self.template = template
        super().__init__(f"Failed to provision sandbox with template '{template}': {message}")


class SandboxConnectionError(Exception):
    """Raised when reconnecting to an existing sandbox fails."""

    def __init__(self, sandbox_id: str, message: str):
        self.sandbox_id = sandbox_id
        super().__init__(f"Failed to connect to sandbox '{sandbox_id}': {message}")


class SandboxProvisioner:
    """Manages E2B sandbox lifecycle: provision, connect, pause, resume, destroy.

    All E2B SDK methods are synchronous. This class wraps them with
    asyncio.to_thread() to avoid blocking the event loop.

    Usage:
        provisioner = SandboxProvisioner()
        sandbox = await provisioner.provision("python-3.11", vcpu=2, memory_mb=2048, timeout_seconds=3600)
        sandbox_id = sandbox.sandbox_id
        # ... use sandbox ...
        await provisioner.destroy(sandbox)
    """

    def __init__(self, api_key: str | None = None):
        """Initialize the provisioner.

        Args:
            api_key: E2B API key. If None, reads from E2B_API_KEY env var.
        """
        self._api_key = api_key or os.environ.get("E2B_API_KEY")
        if not self._api_key:
            raise ValueError("E2B API key not provided and E2B_API_KEY env var not set")

    async def provision(
        self,
        template: str,
        vcpu: int = 2,
        memory_mb: int = 2048,
        timeout_seconds: int = 3600,
    ) -> Sandbox:
        """Provision a new E2B sandbox.

        Retries up to 3 times with exponential backoff (1s, 2s, 4s) on failure.

        Args:
            template: E2B sandbox template (e.g., "python-3.11")
            vcpu: CPU allocation (1-8). Stored in agent config for future use /
                custom template selection, but NOT passed to the current E2B SDK.
                E2B resource allocation is template-based.
            memory_mb: Memory allocation in MB (512-8192). Stored in agent config
                for future use / custom template selection, but NOT passed to the
                current E2B SDK. E2B resource allocation is template-based.
            timeout_seconds: Maximum sandbox lifetime in seconds (60-86400)

        Returns:
            E2B Sandbox instance

        Raises:
            SandboxProvisionError: if provisioning fails after all retries

        Note:
            ``vcpu`` and ``memory_mb`` are accepted for forward compatibility but
            are not sent to E2B. The E2B ``e2b-code-interpreter`` SDK controls
            resources via the template. These parameters are validated in Task 1's
            agent config and may be used for custom template selection in the future.
        """
        last_error: Exception | None = None

        for attempt in range(DEFAULT_RETRY_ATTEMPTS):
            try:
                start_time = time.monotonic()
                sandbox = await asyncio.to_thread(
                    Sandbox.create,
                    template=template,
                    api_key=self._api_key,
                    timeout=timeout_seconds,
                )
                duration_ms = int((time.monotonic() - start_time) * 1000)

                logger.info(
                    "sandbox_provisioned",
                    extra={
                        "sandbox_id": sandbox.sandbox_id,
                        "template": template,
                        "vcpu": vcpu,
                        "memory_mb": memory_mb,
                        "timeout_seconds": timeout_seconds,
                        "duration_ms": duration_ms,
                        "attempt": attempt + 1,
                    },
                )
                return sandbox

            except Exception as e:
                last_error = e
                if attempt < DEFAULT_RETRY_ATTEMPTS - 1:
                    backoff = DEFAULT_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "sandbox_provision_retry",
                        extra={
                            "template": template,
                            "attempt": attempt + 1,
                            "backoff_seconds": backoff,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "sandbox_provision_failed",
                        extra={
                            "template": template,
                            "attempts": DEFAULT_RETRY_ATTEMPTS,
                            "error": str(e),
                        },
                    )

        raise SandboxProvisionError(template, str(last_error))

    async def connect(self, sandbox_id: str) -> Sandbox:
        """Reconnect to an existing sandbox by ID.

        Used for crash recovery — the worker reads sandbox_id from the DB
        and reconnects to continue execution.

        Args:
            sandbox_id: E2B sandbox ID from a previous provision() call

        Returns:
            E2B Sandbox instance

        Raises:
            SandboxConnectionError: if the sandbox cannot be reached (expired, etc.)
        """
        try:
            start_time = time.monotonic()
            sandbox = await asyncio.to_thread(
                Sandbox.connect,
                sandbox_id,
                api_key=self._api_key,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_reconnected",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
            return sandbox

        except Exception as e:
            logger.error(
                "sandbox_reconnect_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            raise SandboxConnectionError(sandbox_id, str(e)) from e

    async def pause(self, sandbox: Sandbox) -> None:
        """Pause a sandbox (stops billing).

        Used when a task enters HITL waiting state. The sandbox filesystem
        is preserved but compute is stopped.

        Args:
            sandbox: E2B Sandbox instance to pause
        """
        sandbox_id = sandbox.sandbox_id
        try:
            start_time = time.monotonic()
            await asyncio.to_thread(sandbox.pause)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_paused",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
        except Exception as e:
            logger.warning(
                "sandbox_pause_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            # Don't raise — sandbox timeout will handle cleanup if pause fails

    async def resume(self, sandbox_id: str) -> Sandbox:
        """Resume a paused sandbox.

        Used when a task resumes from HITL waiting state. E2B auto-resumes
        paused sandboxes on connect.

        Args:
            sandbox_id: E2B sandbox ID of the paused sandbox

        Returns:
            E2B Sandbox instance

        Raises:
            SandboxConnectionError: if the sandbox cannot be resumed
        """
        try:
            start_time = time.monotonic()
            sandbox = await asyncio.to_thread(
                Sandbox.connect,
                sandbox_id,
                api_key=self._api_key,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_resumed",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
            return sandbox

        except Exception as e:
            logger.error(
                "sandbox_resume_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            raise SandboxConnectionError(sandbox_id, str(e)) from e

    async def destroy(self, sandbox: Sandbox) -> None:
        """Destroy a sandbox and release all resources.

        Called on task completion. Best-effort — if destroy fails, E2B
        will auto-expire the sandbox based on its timeout.

        Args:
            sandbox: E2B Sandbox instance to destroy
        """
        sandbox_id = sandbox.sandbox_id
        try:
            start_time = time.monotonic()
            await asyncio.to_thread(sandbox.kill)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            logger.info(
                "sandbox_destroyed",
                extra={
                    "sandbox_id": sandbox_id,
                    "duration_ms": duration_ms,
                },
            )
        except Exception as e:
            logger.warning(
                "sandbox_destroy_failed",
                extra={
                    "sandbox_id": sandbox_id,
                    "error": str(e),
                },
            )
            # Don't raise — E2B auto-expires sandboxes if destroy fails
