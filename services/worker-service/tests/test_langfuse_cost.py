"""Tests for Langfuse per-task credential resolution and cost tracking in GraphExecutor."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.config import WorkerConfig
from executor.graph import GraphExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor() -> GraphExecutor:
    """Create a GraphExecutor with a fully-mocked asyncpg pool."""
    config = WorkerConfig(worker_id="test-worker", worker_pool_id="shared")
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    executor = GraphExecutor(config, pool)
    return executor


# ---------------------------------------------------------------------------
# _extract_tokens
# ---------------------------------------------------------------------------

def test_extract_tokens_anthropic_format():
    metadata = {"usage": {"input_tokens": 100, "output_tokens": 50}}
    assert GraphExecutor._extract_tokens(metadata) == (100, 50)


def test_extract_tokens_openai_format():
    metadata = {"token_usage": {"prompt_tokens": 200, "completion_tokens": 80}}
    assert GraphExecutor._extract_tokens(metadata) == (200, 80)


def test_extract_tokens_bedrock_format():
    metadata = {"usage_metadata": {"input_tokens": 300, "output_tokens": 120}}
    assert GraphExecutor._extract_tokens(metadata) == (300, 120)


def test_extract_tokens_empty_metadata():
    assert GraphExecutor._extract_tokens({}) == (0, 0)


def test_extract_tokens_missing_keys():
    # usage dict present but without the expected token keys
    assert GraphExecutor._extract_tokens({"usage": {"some_other_key": 42}}) == (0, 0)
    # top-level key present but empty
    assert GraphExecutor._extract_tokens({"token_usage": {}}) == (0, 0)


# ---------------------------------------------------------------------------
# _calculate_step_cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calculate_step_cost_with_known_model():
    executor = _make_executor()
    # 3000 microdollars per million input, 15000 per million output
    mock_row = {"input_microdollars_per_million": 3000, "output_microdollars_per_million": 15000}
    executor.pool.fetchrow = AsyncMock(return_value=mock_row)

    metadata = {"usage": {"input_tokens": 1_000_000, "output_tokens": 500_000}}
    cost, exec_meta = await executor._calculate_step_cost(metadata, "claude-3-5-sonnet-latest")

    # cost = (1_000_000 * 3000 + 500_000 * 15000) // 1_000_000
    #       = (3_000_000_000 + 7_500_000_000) // 1_000_000
    #       = 10_500_000_000 // 1_000_000
    #       = 10_500
    assert cost == 10_500


@pytest.mark.asyncio
async def test_calculate_step_cost_unknown_model():
    executor = _make_executor()
    # DB returns no row for the model
    executor.pool.fetchrow = AsyncMock(return_value=None)

    metadata = {"usage": {"input_tokens": 100, "output_tokens": 50}}
    cost, exec_meta = await executor._calculate_step_cost(metadata, "unknown-model-xyz")

    assert cost == 0


@pytest.mark.asyncio
async def test_calculate_step_cost_returns_metadata():
    executor = _make_executor()
    mock_row = {"input_microdollars_per_million": 1000, "output_microdollars_per_million": 2000}
    executor.pool.fetchrow = AsyncMock(return_value=mock_row)

    metadata = {"usage": {"input_tokens": 42, "output_tokens": 17}}
    cost, exec_meta = await executor._calculate_step_cost(metadata, "test-model")

    assert exec_meta["input_tokens"] == 42
    assert exec_meta["output_tokens"] == 17
    assert exec_meta["model"] == "test-model"


# ---------------------------------------------------------------------------
# _resolve_langfuse_credentials
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_credentials_found():
    executor = _make_executor()
    mock_row = {
        "host": "https://langfuse.example.com",
        "public_key": "pk-lf-test-public",
        "secret_key": "sk-lf-test-secret",
    }
    executor.pool.fetchrow = AsyncMock(return_value=mock_row)

    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000001")

    assert result is not None
    assert result["host"] == "https://langfuse.example.com"
    assert result["public_key"] == "pk-lf-test-public"
    assert result["secret_key"] == "sk-lf-test-secret"
    executor.pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_credentials_not_found():
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value=None)

    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000002")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_credentials_db_error():
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(side_effect=Exception("connection refused"))

    # Should degrade gracefully and return None instead of raising
    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000003")

    assert result is None
