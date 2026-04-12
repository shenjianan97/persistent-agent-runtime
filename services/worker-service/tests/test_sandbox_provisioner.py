"""Unit tests for SandboxProvisioner."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sandbox.provisioner import (
    SandboxConnectionError,
    SandboxProvisionError,
    SandboxProvisioner,
)


@pytest.fixture
def provisioner():
    return SandboxProvisioner(api_key="test-api-key")


class TestSandboxProvisionerInit:
    def test_init_with_explicit_key(self):
        p = SandboxProvisioner(api_key="my-key")
        assert p._api_key == "my-key"

    def test_init_with_env_var(self, monkeypatch):
        monkeypatch.setenv("E2B_API_KEY", "env-key")
        p = SandboxProvisioner()
        assert p._api_key == "env-key"

    def test_init_no_key_raises(self, monkeypatch):
        monkeypatch.delenv("E2B_API_KEY", raising=False)
        with pytest.raises(ValueError, match="E2B API key"):
            SandboxProvisioner()


class TestSandboxProvisionerProvision:
    @pytest.mark.asyncio
    async def test_provision_success(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-123"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            result = await provisioner.provision("python-3.11", vcpu=2, memory_mb=2048, timeout_seconds=3600)

        assert result == mock_sandbox
        mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_provision_retries_on_failure(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-456"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = [
                ConnectionError("E2B API down"),
                ConnectionError("E2B API still down"),
                mock_sandbox,
            ]
            with patch("sandbox.provisioner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await provisioner.provision("python-3.11")

        assert result == mock_sandbox
        assert mock_thread.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @pytest.mark.asyncio
    async def test_provision_exhausts_retries_raises(self, provisioner):
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = ConnectionError("E2B API down")
            with patch("sandbox.provisioner.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(SandboxProvisionError, match="python-3.11"):
                    await provisioner.provision("python-3.11")

        assert mock_thread.call_count == 3


class TestSandboxProvisionerConnect:
    @pytest.mark.asyncio
    async def test_connect_success(self, provisioner):
        mock_sandbox = MagicMock()

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            result = await provisioner.connect("sbx-existing-123")

        assert result == mock_sandbox

    @pytest.mark.asyncio
    async def test_connect_failure_raises(self, provisioner):
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Sandbox not found")
            with pytest.raises(SandboxConnectionError, match="sbx-expired"):
                await provisioner.connect("sbx-expired")


class TestSandboxProvisionerPause:
    @pytest.mark.asyncio
    async def test_pause_success(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-pause-123"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock):
            await provisioner.pause(mock_sandbox)
            # Should not raise

    @pytest.mark.asyncio
    async def test_pause_failure_does_not_raise(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-pause-fail"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Pause failed")
            # Should NOT raise — pause failure is logged but swallowed
            await provisioner.pause(mock_sandbox)


class TestSandboxProvisionerResume:
    @pytest.mark.asyncio
    async def test_resume_success(self, provisioner):
        mock_sandbox = MagicMock()

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_sandbox
            result = await provisioner.resume("sbx-paused-123")

        assert result == mock_sandbox

    @pytest.mark.asyncio
    async def test_resume_failure_raises(self, provisioner):
        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Sandbox expired during pause")
            with pytest.raises(SandboxConnectionError, match="sbx-expired"):
                await provisioner.resume("sbx-expired")


class TestSandboxProvisionerDestroy:
    @pytest.mark.asyncio
    async def test_destroy_success(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-destroy-123"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock):
            await provisioner.destroy(mock_sandbox)
            # Should not raise

    @pytest.mark.asyncio
    async def test_destroy_failure_does_not_raise(self, provisioner):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-destroy-fail"

        with patch("sandbox.provisioner.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("Destroy failed")
            # Should NOT raise — destroy failure is logged but swallowed
            await provisioner.destroy(mock_sandbox)


class TestSandboxProvisionError:
    def test_error_message(self):
        err = SandboxProvisionError("python-3.11", "API timeout")
        assert "python-3.11" in str(err)
        assert "API timeout" in str(err)
        assert err.template == "python-3.11"


class TestSandboxConnectionError:
    def test_error_message(self):
        err = SandboxConnectionError("sbx-abc", "not found")
        assert "sbx-abc" in str(err)
        assert "not found" in str(err)
        assert err.sandbox_id == "sbx-abc"
