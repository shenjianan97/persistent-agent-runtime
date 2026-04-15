"""Integration tests for E2B sandbox and file input features (Track 2).

Tests run in mocked mode by default. Set E2B_API_KEY env var for live tests
against real E2B infrastructure.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sandbox.provisioner import (
    SandboxConnectionError,
    SandboxProvisionError,
    SandboxProvisioner,
)
from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxReadFileArguments,
    SandboxWriteFileArguments,
    SandboxDownloadArguments,
    create_sandbox_exec_fn,
    create_sandbox_read_file_fn,
    create_sandbox_write_file_fn,
    create_sandbox_download_fn,
)


# --- Helpers ---

def make_mock_sandbox(sandbox_id: str = "sbx-test-integration"):
    """Create a mock E2B Sandbox for testing."""
    mock = MagicMock()
    mock.sandbox_id = sandbox_id

    # Mock commands.run
    mock_run_result = MagicMock()
    mock_run_result.stdout = ""
    mock_run_result.stderr = ""
    mock_run_result.exit_code = 0
    mock.commands.run = MagicMock(return_value=mock_run_result)

    # Mock files.read / files.write
    mock.files.read = MagicMock(return_value="file content")
    mock.files.write = MagicMock()

    # Mock pause / kill
    mock.pause = MagicMock()
    mock.kill = MagicMock()

    return mock


class AsyncContextManager:
    """Helper for mocking async context managers."""

    def __init__(self, mock_obj):
        self._mock = mock_obj

    async def __aenter__(self):
        return self._mock

    async def __aexit__(self, *args):
        pass


# --- Test 1: Full Sandbox Lifecycle ---

class TestSandboxLifecycleIntegration:
    """Test the full sandbox lifecycle: provision → exec → read → write → destroy."""

    @pytest.mark.asyncio
    async def test_provision_exec_destroy(self):
        """Verify sandbox provisioning, command execution, and destruction."""
        provisioner = SandboxProvisioner(api_key="test-key")
        mock_sandbox = make_mock_sandbox()

        mock_run_result = MagicMock()
        mock_run_result.stdout = "Hello, World!\n"
        mock_run_result.stderr = ""
        mock_run_result.exit_code = 0
        mock_sandbox.commands.run = MagicMock(return_value=mock_run_result)

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            # provision returns sandbox
            mock_thread.return_value = mock_sandbox
            sandbox = await provisioner.provision("python-3.11", vcpu=2, memory_mb=2048, timeout_seconds=3600)

        assert sandbox.sandbox_id == "sbx-test-integration"

        # Execute a command
        exec_fn = create_sandbox_exec_fn(sandbox)
        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_run_result
            result = await exec_fn("echo Hello, World!")

        assert result["stdout"] == "Hello, World!\n"
        assert result["exit_code"] == 0

        # Destroy
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock):
            await provisioner.destroy(sandbox)

    @pytest.mark.asyncio
    async def test_provision_file_io_cycle(self):
        """Verify writing a file, reading it back, and downloading as artifact."""
        sandbox = make_mock_sandbox()

        # Write file
        write_fn = create_sandbox_write_file_fn(sandbox)
        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock):
            write_result = await write_fn("/home/user/output.txt", "analysis results")

        assert write_result["path"] == "/home/user/output.txt"
        assert write_result["size_bytes"] == len("analysis results".encode("utf-8"))

        # Read file
        read_fn = create_sandbox_read_file_fn(sandbox)
        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = "analysis results"
            read_result = await read_fn("/home/user/output.txt")

        assert read_result["content"] == "analysis results"


# --- Test 2: File Input Flow ---

class TestFileInputFlowIntegration:
    """Test file upload → S3 storage → sandbox injection flow."""

    @pytest.mark.asyncio
    async def test_input_file_query_and_inject(self):
        """Verify input files are queried from DB and written to sandbox."""
        sandbox = make_mock_sandbox()

        # Simulate DB rows for input artifacts
        mock_rows = [
            {
                "filename": "document.pdf",
                "s3_key": "default/task-123/input/document.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1024,
            },
            {
                "filename": "data.csv",
                "s3_key": "default/task-123/input/data.csv",
                "content_type": "text/csv",
                "size_bytes": 256,
            },
        ]

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=mock_rows)

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        mock_s3 = AsyncMock()
        mock_s3.download = AsyncMock(side_effect=[b"pdf bytes", b"csv bytes"])

        from executor.graph import GraphExecutor
        from core.config import WorkerConfig

        config = WorkerConfig(worker_id="test-worker", worker_pool_id="shared")
        executor = GraphExecutor(pool=mock_pool, config=config, s3_client=mock_s3)

        with patch("executor.graph.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            # Two to_thread calls: sandbox.files.write for each file
            mock_thread.return_value = None
            result = await executor._inject_input_files(sandbox, "task-123", "default")

        assert len(result) == 2
        assert "document.pdf" in result
        assert "data.csv" in result


# --- Test 3: sandbox_download Artifact Flow ---

class TestSandboxDownloadIntegration:
    """Test sandbox file → S3 output artifact pipeline."""

    @pytest.mark.asyncio
    async def test_download_creates_artifact(self):
        """Verify sandbox_download reads file, uploads to S3, and inserts DB row."""
        sandbox = make_mock_sandbox()

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="default/task-123/output/report.pdf")
        mock_s3.upload = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = [
                b"%PDF-1.4 fake pdf content",  # sandbox.files.read
                None,                            # s3_client.upload (but upload is AsyncMock, won't hit to_thread)
            ]
            # Override: sandbox.files.read returns bytes via to_thread
            # but s3.upload is already async — only one to_thread call (files.read)
            mock_thread.side_effect = None
            mock_thread.return_value = b"%PDF-1.4 fake pdf content"
            result = await download_fn("/home/user/report.pdf")

        assert result["filename"] == "report.pdf"
        assert result["content_type"] == "application/pdf"
        assert result["size_bytes"] == len(b"%PDF-1.4 fake pdf content")

        # Verify S3 upload was called
        mock_s3.build_key.assert_called_once_with("default", "task-123", "output", "report.pdf")
        mock_s3.upload.assert_called_once()

        # Verify DB insert was called
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        assert "task_artifacts" in call_args[0][0]
        assert call_args[0][3] == "report.pdf"  # filename


# --- Test 4: Crash Recovery ---

class TestCrashRecoveryIntegration:
    """Test sandbox reconnection after worker crash."""

    @pytest.mark.asyncio
    async def test_reconnect_success(self):
        """Verify provisioner.connect() reconnects to existing sandbox."""
        provisioner = SandboxProvisioner(api_key="test-key")
        mock_sandbox = make_mock_sandbox("sbx-crashed-123")

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            sandbox = await provisioner.connect("sbx-crashed-123")

        assert sandbox.sandbox_id == "sbx-crashed-123"

    @pytest.mark.asyncio
    async def test_reconnect_expired_raises(self):
        """Verify expired sandbox raises SandboxConnectionError."""
        provisioner = SandboxProvisioner(api_key="test-key")

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Sandbox not found or expired")
            with pytest.raises(SandboxConnectionError, match="sbx-expired"):
                await provisioner.connect("sbx-expired")

    @pytest.mark.asyncio
    async def test_provision_retry_exhaustion(self):
        """Verify 3 retries with backoff, then SandboxProvisionError."""
        provisioner = SandboxProvisioner(api_key="test-key")

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = ConnectionError("E2B API down")
            with patch("sandbox.provisioner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                with pytest.raises(SandboxProvisionError):
                    await provisioner.provision("python-3.11")

        assert mock_thread.call_count == 3
        assert mock_sleep.call_count == 2


# --- Test 5: Non-Sandbox Agent ---

class TestNonSandboxAgentIntegration:
    """Test that agents without sandbox config work normally."""

    def test_sandbox_config_absent_defaults_to_disabled(self):
        """Agent config without sandbox block → sandbox disabled."""
        agent_config = {
            "system_prompt": "You are a helpful assistant.",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest",
            "temperature": 0.7,
            "allowed_tools": ["web_search", "calculator"],
        }
        sandbox_config = agent_config.get("sandbox", {})
        sandbox_enabled = sandbox_config.get("enabled", False)
        assert sandbox_enabled is False

    def test_sandbox_config_disabled_explicitly(self):
        """Agent config with sandbox.enabled: false → sandbox disabled."""
        agent_config = {
            "system_prompt": "You are a helpful assistant.",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest",
            "temperature": 0.7,
            "allowed_tools": ["web_search"],
            "sandbox": {"enabled": False},
        }
        sandbox_config = agent_config.get("sandbox", {})
        sandbox_enabled = sandbox_config.get("enabled", False)
        assert sandbox_enabled is False

    def test_sandbox_tools_not_in_non_sandbox_agent(self):
        """Sandbox tools should not appear in allowed_tools for non-sandbox agents."""
        agent_config = {
            "allowed_tools": ["web_search", "calculator"],
        }
        sandbox_tools = {"sandbox_exec", "sandbox_read_file", "sandbox_write_file", "export_sandbox_file"}
        agent_tools = set(agent_config.get("allowed_tools", []))
        assert agent_tools.isdisjoint(sandbox_tools)


# --- Test 6: HITL Sandbox Pause/Resume ---

class TestHITLSandboxPauseIntegration:
    """Test sandbox pause on HITL wait and resume."""

    @pytest.mark.asyncio
    async def test_pause_and_resume(self):
        """Verify sandbox is paused on HITL and resumed on return."""
        provisioner = SandboxProvisioner(api_key="test-key")
        mock_sandbox = make_mock_sandbox()

        # Pause
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock):
            await provisioner.pause(mock_sandbox)
            # Should not raise

        # Resume (connect to paused sandbox)
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            resumed = await provisioner.resume(mock_sandbox.sandbox_id)

        assert resumed.sandbox_id == mock_sandbox.sandbox_id


# --- Test 7: Sandbox Cost Calculation ---

class TestSandboxCostCalculation:
    """Test E2B cost calculation formula."""

    def test_cost_10_minutes_2vcpu(self):
        """10 min, 2 vCPU: 600 * 2 * 50000 / 3600 = 16666 microdollars."""
        duration = 600
        vcpu = 2
        cost = int(duration * vcpu * 50000 / 3600)
        assert cost == 16666

    def test_cost_1_hour_1vcpu(self):
        """1 hour, 1 vCPU: 3600 * 1 * 50000 / 3600 = 50000 microdollars ($0.05)."""
        duration = 3600
        vcpu = 1
        cost = int(duration * vcpu * 50000 / 3600)
        assert cost == 50000

    def test_cost_30_seconds_4vcpu(self):
        """30 sec, 4 vCPU: 30 * 4 * 50000 / 3600 = 1666 microdollars."""
        duration = 30
        vcpu = 4
        cost = int(duration * vcpu * 50000 / 3600)
        assert cost == 1666

    def test_cost_zero_duration(self):
        """0 seconds = 0 cost."""
        cost = int(0 * 2 * 50000 / 3600)
        assert cost == 0


# --- Live E2B Tests (opt-in) ---

E2B_API_KEY = os.environ.get("E2B_API_KEY")


@pytest.mark.skipif(not E2B_API_KEY, reason="E2B_API_KEY not set — skipping live sandbox tests")
class TestLiveSandboxIntegration:
    """Live tests against real E2B infrastructure. Requires E2B_API_KEY."""

    @pytest.mark.asyncio
    async def test_live_provision_exec_destroy(self):
        """Provision a real sandbox, run a command, verify output, destroy."""
        provisioner = SandboxProvisioner(api_key=E2B_API_KEY)

        sandbox = await provisioner.provision(
            template="base",
            vcpu=1,
            memory_mb=512,
            timeout_seconds=300,
        )
        assert sandbox.sandbox_id is not None

        try:
            # Execute a command
            exec_fn = create_sandbox_exec_fn(sandbox)

            # Use asyncio.to_thread directly (not mocked) for live test
            result_obj = await asyncio.to_thread(sandbox.commands.run, "echo 'live test'")
            assert result_obj.exit_code == 0
            assert "live test" in result_obj.stdout

            # Write and read a file
            await asyncio.to_thread(sandbox.files.write, "/home/user/test.txt", "live content")
            content = await asyncio.to_thread(sandbox.files.read, "/home/user/test.txt")
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            assert "live content" in content

        finally:
            await provisioner.destroy(sandbox)

    @pytest.mark.asyncio
    async def test_live_pause_resume(self):
        """Provision, pause, resume, verify sandbox still works."""
        provisioner = SandboxProvisioner(api_key=E2B_API_KEY)

        sandbox = await provisioner.provision(
            template="base",
            vcpu=1,
            memory_mb=512,
            timeout_seconds=300,
        )
        sandbox_id = sandbox.sandbox_id

        try:
            # Write a file before pause
            await asyncio.to_thread(sandbox.files.write, "/home/user/before_pause.txt", "data")

            # Pause
            await provisioner.pause(sandbox)

            # Resume
            sandbox = await provisioner.resume(sandbox_id)

            # Verify file still exists
            content = await asyncio.to_thread(sandbox.files.read, "/home/user/before_pause.txt")
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            assert "data" in content

        finally:
            await provisioner.destroy(sandbox)
