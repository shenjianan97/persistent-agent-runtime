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
# _extract_token_usage
# ---------------------------------------------------------------------------

def _basic(metadata: dict, provider: str = "noop"):
    usage = GraphExecutor._extract_token_usage(metadata, provider)
    return (usage.input_tokens, usage.output_tokens)


def test_extract_tokens_anthropic():
    metadata = {"usage": {"input_tokens": 100, "output_tokens": 50}}
    # The anthropic strategy reads the native ``usage`` shape directly.
    assert _basic(metadata, "anthropic") == (100, 50)


def test_extract_tokens_openai():
    metadata = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    assert _basic(metadata, "openai") == (100, 50)


def test_extract_tokens_bedrock():
    metadata = {"usage_metadata": {"input_tokens": 100, "output_tokens": 50}}
    assert _basic(metadata, "bedrock") == (100, 50)


def test_extract_tokens_empty():
    assert _basic({}, "noop") == (0, 0)


def test_extract_tokens_partial():
    """Only input_tokens present, no output key — output should fall back to 0."""
    metadata = {"usage": {"input_tokens": 100}}
    assert _basic(metadata, "noop") == (100, 0)


def test_extract_tokens_anthropic_cache_counters():
    """Anthropic cache hit → cache_read_input_tokens populated; cost path
    sees the non-cached portion of input as ``input_tokens``."""
    metadata = {
        "usage": {
            "input_tokens": 20,
            "output_tokens": 50,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 1000,
        }
    }
    usage = GraphExecutor._extract_token_usage(metadata, "anthropic")
    assert usage.input_tokens == 20
    assert usage.output_tokens == 50
    assert usage.cache_creation_input_tokens == 5
    assert usage.cache_read_input_tokens == 1000
    assert usage.total_prompt_tokens == 1025


def test_extract_tokens_openai_cached_tokens():
    """OpenAI reports cached hits via prompt_tokens_details.cached_tokens;
    the strategy normalises ``input_tokens`` to the non-cached portion."""
    metadata = {
        "token_usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 1000},
        }
    }
    usage = GraphExecutor._extract_token_usage(metadata, "openai")
    assert usage.input_tokens == 200
    assert usage.cache_read_input_tokens == 1000
    assert usage.output_tokens == 50


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
        "cache_creation_microdollars_per_million": None,
        "cache_read_microdollars_per_million": None,
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
        "cache_creation_microdollars_per_million": None,
        "cache_read_microdollars_per_million": None,
    })

    model = "test-model"
    metadata = {"usage": {"input_tokens": 42, "output_tokens": 17}}

    _, exec_meta = await executor._calculate_step_cost(metadata, model)

    assert exec_meta["input_tokens"] == 42
    assert exec_meta["output_tokens"] == 17
    assert exec_meta["model"] == model


@pytest.mark.asyncio
async def test_calculate_cost_null_cache_rates_default_to_zero():
    """A row predating migration 0022 (or any model not yet re-seeded by
    model-discovery) has NULL cache rates. The previous fallback charged
    cache reads at the full input rate — which is 10× Anthropic's real
    rate and would silently over-bill tenants on every cache hit. New
    contract: NULL → 0, and emit a single warning per (model, bucket).
    """
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value={
        "input_microdollars_per_million": 3_000_000,
        "output_microdollars_per_million": 15_000_000,
        "cache_creation_microdollars_per_million": None,
        "cache_read_microdollars_per_million": None,
    })

    metadata = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 5_000,
        }
    }
    cost, exec_meta = await executor._calculate_step_cost(
        metadata, "claude-haiku-4-5", provider="anthropic"
    )

    # cache_creation + cache_read contribute $0 when rates are NULL; only
    # the uncached input and output portions bill. Previously this would
    # have been (100 + 200 + 5000) * 3_000_000 → 10x over-charge on the
    # cache bucket.
    expected = (100 * 3_000_000 + 50 * 15_000_000) // 1_000_000
    assert cost == expected
    # Counters still surface in execution_metadata for observability.
    assert exec_meta["cache_creation_input_tokens"] == 200
    assert exec_meta["cache_read_input_tokens"] == 5_000


@pytest.mark.asyncio
async def test_calculate_cost_honors_populated_cache_rates():
    """When the row has real cache pricing, the four buckets price
    independently — regression guard against the NULL fix also zeroing
    out seeded rates."""
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value={
        "input_microdollars_per_million": 3_000_000,
        "output_microdollars_per_million": 15_000_000,
        "cache_creation_microdollars_per_million": 3_750_000,  # 1.25x
        "cache_read_microdollars_per_million": 300_000,         # 0.10x
    })

    metadata = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 5_000,
        }
    }
    cost, _ = await executor._calculate_step_cost(
        metadata, "claude-haiku-4-5", provider="anthropic"
    )
    expected = (
        100 * 3_000_000
        + 50 * 15_000_000
        + 200 * 3_750_000
        + 5_000 * 300_000
    ) // 1_000_000
    assert cost == expected


@pytest.mark.asyncio
async def test_missing_cache_rate_warning_deduped_per_model_bucket(caplog):
    """The missing-cache-rate log fires at most once per (model, bucket)
    within the executor's lifetime. Agents looping with NULL rates would
    otherwise bury the log."""
    import logging
    executor = _make_executor()
    executor.pool.fetchrow = AsyncMock(return_value={
        "input_microdollars_per_million": 3_000_000,
        "output_microdollars_per_million": 15_000_000,
        "cache_creation_microdollars_per_million": None,
        "cache_read_microdollars_per_million": None,
    })

    metadata = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 10,
            "cache_read_input_tokens": 100,
        }
    }

    with caplog.at_level(logging.WARNING, logger="executor.graph"):
        for _ in range(5):
            await executor._calculate_step_cost(
                metadata, "claude-haiku-4-5", provider="anthropic"
            )

    matched = [
        r for r in caplog.records
        if "prompt_cache.missing_rate" in r.getMessage()
        and "cache_read" in r.getMessage()
    ]
    assert len(matched) == 1


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
