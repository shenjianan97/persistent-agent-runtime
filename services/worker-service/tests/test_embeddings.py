"""Tests for the Phase 2 Track 5 worker-side embedding helper.

Covers:
- Happy-path returns EmbeddingResult with the expected 1536-d vector and token count.
- Provider timeout / connection failure → None (no raise).
- 5xx → retries once, then None.
- Malformed response (wrong dimension / missing fields) → None.
- No input text appears in emitted log lines.

The helper itself reads credentials from the `provider_keys` table; in these
tests we stub the key lookup via the injected `api_key_loader` hook and stub
the HTTP client with `httpx.MockTransport` so the unit tests never touch the
network or Postgres.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from executor import embeddings as embeddings_mod
from executor.embeddings import (
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingResult,
    compute_embedding,
)


def _mock_client(handler) -> httpx.AsyncClient:
    """httpx.AsyncClient backed by a MockTransport. MockTransport routes every
    request through `handler` without opening a socket — suitable for unit
    tests that need to assert on request payloads or fabricate failure modes.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fake_key_loader() -> str | None:
    return "test-api-key"


async def _no_key_loader() -> str | None:
    return None


class TestComputeEmbeddingHappyPath:
    @pytest.mark.asyncio
    async def test_returns_vector_and_tokens_on_success(self) -> None:
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            # The helper should POST to the OpenAI embeddings endpoint with
            # the configured model and input.
            assert request.url.path.endswith("/v1/embeddings")
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1] * DEFAULT_EMBEDDING_DIMENSION}],
                    "model": DEFAULT_EMBEDDING_MODEL,
                    "usage": {"prompt_tokens": 7},
                },
            )

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello world",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert isinstance(result, EmbeddingResult)
        assert len(result.vector) == DEFAULT_EMBEDDING_DIMENSION
        assert result.tokens == 7
        assert result.cost_microdollars >= 0
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_emits_success_log_without_input_text(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=embeddings_mod.logger.name)

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0] * DEFAULT_EMBEDDING_DIMENSION}],
                    "usage": {"prompt_tokens": 3},
                },
            )

        secret_text = "SECRET-INPUT-TEXT-should-never-appear-in-logs"
        async with _mock_client(handler) as client:
            await compute_embedding(
                secret_text,
                client=client,
                api_key_loader=_fake_key_loader,
            )

        succeeded = [r for r in caplog.records if "memory.embedding.succeeded" in r.getMessage()]
        assert succeeded, "expected memory.embedding.succeeded log line"
        for record in caplog.records:
            assert secret_text not in record.getMessage()
            # Also not in any extra attribute — defense in depth.
            for value in record.__dict__.values():
                assert secret_text != value


class TestComputeEmbeddingProviderDown:
    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow provider", request=request)

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_connection_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down", request=request)

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_5xx_retries_once_then_none(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(502, json={"error": "upstream"})

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is None
        # Original attempt + exactly one retry.
        assert calls == 2

    @pytest.mark.asyncio
    async def test_5xx_then_success_recovers(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0] * DEFAULT_EMBEDDING_DIMENSION}],
                    "usage": {"prompt_tokens": 2},
                },
            )

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is not None
        assert calls == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_no_api_key(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("HTTP should not be called without an API key")

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_no_key_loader,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_failure_log_does_not_contain_input_text(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=embeddings_mod.logger.name)

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        secret_text = "PII-LADEN-TEXT-should-never-appear-in-logs"
        async with _mock_client(handler) as client:
            await compute_embedding(
                secret_text,
                client=client,
                api_key_loader=_fake_key_loader,
            )

        failed = [r for r in caplog.records if "memory.embedding.failed" in r.getMessage()]
        assert failed, "expected memory.embedding.failed log line"
        for record in caplog.records:
            assert secret_text not in record.getMessage()
            for value in record.__dict__.values():
                assert secret_text != value


class TestComputeEmbeddingMalformedResponse:
    @pytest.mark.asyncio
    async def test_wrong_dimension_returns_none(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.0] * 100}],  # wrong dim
                    "usage": {"prompt_tokens": 1},
                },
            )

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_data_array_returns_none(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, json={"usage": {"prompt_tokens": 1}})

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_non_numeric_vector_returns_none(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": ["NaN-as-string"] * DEFAULT_EMBEDDING_DIMENSION}],
                    "usage": {"prompt_tokens": 1},
                },
            )

        async with _mock_client(handler) as client:
            result = await compute_embedding(
                "hello",
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_usage_falls_back_to_approximation(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={"data": [{"embedding": [0.0] * DEFAULT_EMBEDDING_DIMENSION}]},
            )

        async with _mock_client(handler) as client:
            # Input length 12 → ceil(12/4) = 3 approximated tokens.
            result = await compute_embedding(
                "x" * 12,
                client=client,
                api_key_loader=_fake_key_loader,
            )

        assert result is not None
        assert result.tokens == 3
