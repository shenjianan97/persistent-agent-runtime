"""Unit tests for sandbox tools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxExecResult,
    SandboxReadFileArguments,
    SandboxReadFileResult,
    SandboxWriteFileArguments,
    SandboxWriteFileResult,
    SandboxDownloadArguments,
    SandboxDownloadResult,
    create_sandbox_exec_fn,
    create_sandbox_read_file_fn,
    create_sandbox_write_file_fn,
    create_sandbox_download_fn,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
)


# Helper for async context manager mocking
class AsyncContextManager:
    def __init__(self, mock_obj):
        self._mock = mock_obj

    async def __aenter__(self):
        return self._mock

    async def __aexit__(self, *args):
        pass


# =============================================================================
# Task 3: sandbox_exec
# =============================================================================

class TestSandboxExecArguments:
    def test_valid_command(self):
        args = SandboxExecArguments(command="echo hello")
        assert args.command == "echo hello"

    def test_empty_command_rejected(self):
        with pytest.raises(Exception):
            SandboxExecArguments(command="")

    def test_long_command_rejected(self):
        with pytest.raises(Exception):
            SandboxExecArguments(command="x" * 10001)

    def test_max_length_command_accepted(self):
        args = SandboxExecArguments(command="x" * 10000)
        assert len(args.command) == 10000


class TestSandboxExecResult:
    def test_result_fields(self):
        result = SandboxExecResult(stdout="hello\n", stderr="", exit_code=0)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0


class TestCreateSandboxExecFn:
    @pytest.mark.asyncio
    async def test_exec_success(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"
        mock_result = MagicMock()
        mock_result.stdout = "hello world"
        mock_result.stderr = ""
        mock_result.exit_code = 0
        mock_sandbox.commands.run = MagicMock(return_value=mock_result)

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_result
            result = await exec_fn("echo hello world")

        assert result["stdout"] == "hello world"
        assert result["stderr"] == ""
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_exec_with_stderr(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "command not found"
        mock_result.exit_code = 127
        mock_sandbox.commands.run = MagicMock(return_value=mock_result)

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_result
            result = await exec_fn("nonexistent-command")

        assert result["stderr"] == "command not found"
        assert result["exit_code"] == 127

    @pytest.mark.asyncio
    async def test_exec_timeout(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        exec_fn = create_sandbox_exec_fn(mock_sandbox, command_timeout=1)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = asyncio.TimeoutError()
            result = await exec_fn("sleep 999")

        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exec_error(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = RuntimeError("Sandbox connection lost")
            result = await exec_fn("echo test")

        assert result["exit_code"] == -1
        assert "Sandbox connection lost" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exec_null_stdout_stderr(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"
        mock_result = MagicMock()
        mock_result.stdout = None
        mock_result.stderr = None
        mock_result.exit_code = 0

        exec_fn = create_sandbox_exec_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_result
            result = await exec_fn("true")

        assert result["stdout"] == ""
        assert result["stderr"] == ""
        assert result["exit_code"] == 0

    def test_default_timeout(self):
        assert DEFAULT_COMMAND_TIMEOUT_SECONDS == 300


# =============================================================================
# Task 4: sandbox_read_file + sandbox_write_file
# =============================================================================

class TestSandboxReadFileArguments:
    def test_valid_path(self):
        args = SandboxReadFileArguments(path="/home/user/data.csv")
        assert args.path == "/home/user/data.csv"

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            SandboxReadFileArguments(path="")

    def test_long_path_rejected(self):
        with pytest.raises(Exception):
            SandboxReadFileArguments(path="x" * 1001)


class TestSandboxReadFileResult:
    def test_result_fields(self):
        result = SandboxReadFileResult(path="/home/user/file.txt", content="hello")
        assert result.path == "/home/user/file.txt"
        assert result.content == "hello"


class TestCreateSandboxReadFileFn:
    @pytest.mark.asyncio
    async def test_read_text_file(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = "file content here"
            result = await read_fn("/home/user/file.txt")

        assert result["path"] == "/home/user/file.txt"
        assert result["content"] == "file content here"

    @pytest.mark.asyncio
    async def test_read_binary_file_returns_message(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"\x89PNG\r\n\x1a\n\x00\x00"
            result = await read_fn("/home/user/image.png")

        assert "Binary file" in result["content"]
        assert "sandbox_download" in result["content"]

    @pytest.mark.asyncio
    async def test_read_bytes_utf8_decodable(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"utf8 text content"
            result = await read_fn("/home/user/file.txt")

        assert result["content"] == "utf8 text content"

    @pytest.mark.asyncio
    async def test_read_file_error(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        read_fn = create_sandbox_read_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = FileNotFoundError("No such file")
            result = await read_fn("/home/user/missing.txt")

        assert "Error reading file" in result["content"]
        assert "No such file" in result["content"]


class TestSandboxWriteFileArguments:
    def test_valid_write(self):
        args = SandboxWriteFileArguments(path="/home/user/output.txt", content="hello world")
        assert args.path == "/home/user/output.txt"
        assert args.content == "hello world"

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            SandboxWriteFileArguments(path="", content="data")

    def test_long_path_rejected(self):
        with pytest.raises(Exception):
            SandboxWriteFileArguments(path="x" * 1001, content="data")


class TestSandboxWriteFileResult:
    def test_result_fields(self):
        result = SandboxWriteFileResult(path="/home/user/output.txt", size_bytes=11)
        assert result.path == "/home/user/output.txt"
        assert result.size_bytes == 11


class TestCreateSandboxWriteFileFn:
    @pytest.mark.asyncio
    async def test_write_success(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            result = await write_fn("/home/user/output.txt", "hello world")

        assert result["path"] == "/home/user/output.txt"
        assert result["size_bytes"] == len("hello world".encode("utf-8"))
        mock_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_empty_content(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock):
            result = await write_fn("/home/user/empty.txt", "")

        assert result["size_bytes"] == 0

    @pytest.mark.asyncio
    async def test_write_error(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = PermissionError("Permission denied")
            result = await write_fn("/root/protected.txt", "data")

        assert result["size_bytes"] == 0

    @pytest.mark.asyncio
    async def test_write_unicode_content(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        write_fn = create_sandbox_write_file_fn(mock_sandbox)

        unicode_content = "Hello, \u4e16\u754c! \U0001f600"

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock):
            result = await write_fn("/home/user/unicode.txt", unicode_content)

        assert result["size_bytes"] == len(unicode_content.encode("utf-8"))


# =============================================================================
# Task 5: sandbox_download
# =============================================================================

class TestSandboxDownloadArguments:
    def test_valid_args(self):
        args = SandboxDownloadArguments(path="/home/user/report.pdf")
        assert args.path == "/home/user/report.pdf"
        assert args.filename is None

    def test_with_custom_filename(self):
        args = SandboxDownloadArguments(path="/home/user/output.txt", filename="report.txt")
        assert args.filename == "report.txt"

    def test_empty_path_rejected(self):
        with pytest.raises(Exception):
            SandboxDownloadArguments(path="")

    def test_long_filename_rejected(self):
        with pytest.raises(Exception):
            SandboxDownloadArguments(path="/file.txt", filename="x" * 256)


class TestSandboxDownloadResult:
    def test_result_fields(self):
        result = SandboxDownloadResult(filename="report.pdf", size_bytes=1024, content_type="application/pdf")
        assert result.filename == "report.pdf"
        assert result.size_bytes == 1024
        assert result.content_type == "application/pdf"


class TestCreateSandboxDownloadFn:
    @pytest.mark.asyncio
    async def test_download_success(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="default/task-123/output/report.pdf")
        mock_s3.upload = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"PDF content here"  # sandbox.files.read
            result = await download_fn("/home/user/report.pdf")

        assert result["filename"] == "report.pdf"
        assert result["size_bytes"] == len(b"PDF content here")
        assert result["content_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_download_custom_filename(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="default/task-123/output/custom.csv")
        mock_s3.upload = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"csv data"  # sandbox.files.read
            result = await download_fn("/home/user/data.txt", "custom.csv")

        assert result["filename"] == "custom.csv"
        assert result["content_type"] == "text/csv"

    @pytest.mark.asyncio
    async def test_download_text_content_encoded(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="key")
        mock_s3.upload = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = "text content"  # str returned from sandbox.files.read
            result = await download_fn("/home/user/file.txt")

        assert result["size_bytes"] == len("text content".encode("utf-8"))

    @pytest.mark.asyncio
    async def test_download_error_returns_result(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_pool = AsyncMock()

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = FileNotFoundError("No such file")
            result = await download_fn("/home/user/missing.txt")

        assert result["size_bytes"] == 0
        assert result["filename"] == "missing.txt"

    @pytest.mark.asyncio
    async def test_download_unknown_extension(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="key")
        mock_s3.upload = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"data"  # sandbox.files.read
            result = await download_fn("/home/user/file.xyz123")

        assert result["content_type"] == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_download_db_insert_called(self):
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_id = "sbx-test"

        mock_s3 = MagicMock()
        mock_s3.build_key = MagicMock(return_value="default/task-123/output/output.txt")
        mock_s3.upload = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncContextManager(mock_conn))

        download_fn = create_sandbox_download_fn(
            mock_sandbox,
            s3_client=mock_s3,
            pool=mock_pool,
            task_id="task-123",
            tenant_id="default",
        )

        with patch("tools.sandbox_tools.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = b"hello"
            await download_fn("/home/user/output.txt")

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        # Check key parameters in the execute call
        assert "task_id" in call_args[0][0] or "$1" in call_args[0][0]
        assert call_args[0][1] == "task-123"  # task_id
        assert call_args[0][2] == "default"   # tenant_id
        assert call_args[0][3] == "output.txt"  # filename
