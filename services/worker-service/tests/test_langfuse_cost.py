"""Unit tests for Langfuse per-task credential resolution and cost tracking in GraphExecutor."""

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
    return GraphExecutor(config, pool)


# ---------------------------------------------------------------------------
# _extract_tokens
# ---------------------------------------------------------------------------

def test_extract_tokens_anthropic():
    metadata = {"usage": {"input_tokens": 100, "output_tokens": 50}}
    assert GraphExecutor._extract_tokens(metadata) == (100, 50)


def test_extract_tokens_openai():
    metadata = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    assert GraphExecutor._extract_tokens(metadata) == (100, 50)


def test_extract_tokens_bedrock():
    metadata = {"usage_metadata": {"input_tokens": 100, "output_tokens": 50}}
    assert GraphExecutor._extract_tokens(metadata) == (100, 50)


def test_extract_tokens_empty():
    assert GraphExecutor._extract_tokens({}) == (0, 0)


def test_extract_tokens_partial():
    """Only input_tokens present, no output key — output should fall back to 0."""
    metadata = {"usage": {"input_tokens": 100}}
    assert GraphExecutor._extract_tokens(metadata) == (100, 0)


# ---------------------------------------------------------------------------
# _calculate_step_cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calculate_cost_known_model():
    executor = _make_executor()
    input_rate = 3_000_000   # microdollars per million tokens
    output_rate = 15_000_000
    executor.pool.fetchrow = AsyncMock(return_value={
        "input_microdollars_per_million": input_rate,
        "output_microdollars_per_million": output_rate,
    })

    input_tokens = 200
    output_tokens = 100
    metadata = {"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}

    cost, _ = await executor._calculate_step_cost(metadata, "claude-3-5-sonnet-latest")

    expected = (input_tokens * input_rate + output_tokens * output_rate) // 1_000_000
    assert cost == expected


@pytest.mark.asyncio
async def test_calculate_cost_unknown_model():
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value=None)

    metadata = {"usage": {"input_tokens": 100, "output_tokens": 50}}
    cost, _ = await executor._calculate_step_cost(metadata, "unknown-model-xyz")

    assert cost == 0


@pytest.mark.asyncio
async def test_calculate_cost_returns_metadata():
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value={
        "input_microdollars_per_million": 1_000,
        "output_microdollars_per_million": 2_000,
    })

    model = "test-model"
    metadata = {"usage": {"input_tokens": 42, "output_tokens": 17}}

    _, exec_meta = await executor._calculate_step_cost(metadata, model)

    assert exec_meta["input_tokens"] == 42
    assert exec_meta["output_tokens"] == 17
    assert exec_meta["model"] == model


# ---------------------------------------------------------------------------
# _resolve_langfuse_credentials
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_found():
    executor = _make_executor()
    # Loopback host — the url_safety re-check accepts it (dev-friendly) and does
    # not hit real DNS.
    executor.pool.fetchrow = AsyncMock(return_value={
        "host": "http://127.0.0.1:3300",
        "public_key": "pk-lf-test-public",
        "secret_key": "sk-lf-test-secret",
    })

    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000001")

    assert result is not None
    assert result["host"] == "http://127.0.0.1:3300"
    assert result["public_key"] == "pk-lf-test-public"
    assert result["secret_key"] == "sk-lf-test-secret"
    executor.pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_rejects_unsafe_host():
    """A stored host that now resolves to a metadata IP must not return creds —
    tracing just degrades off rather than shipping the bearer credentials
    somewhere unsafe."""
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value={
        "host": "http://169.254.169.254/",
        "public_key": "pk-lf-test-public",
        "secret_key": "sk-lf-test-secret",
    })

    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000004")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_not_found():
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value=None)

    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000002")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_db_error():
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(side_effect=Exception("connection refused"))

    # Should degrade gracefully and return None instead of raising
    result = await executor._resolve_langfuse_credentials("aaaabbbb-0000-0000-0000-000000000003")

    assert result is None
