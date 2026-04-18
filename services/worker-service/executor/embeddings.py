"""Worker-side embedding provider abstraction (Phase 2 Track 5).

A single helper, :func:`compute_embedding`, computes an embedding for a piece
of text using the platform-default embedding provider. The helper is the
entry point used by the ``memory_write`` graph node (Task 6) and the
dead-letter memory hook (Task 8).

Design contract (see ``docs/design-docs/phase-2/track-5-memory.md`` and
``docs/exec-plans/active/phase-2/track-5/agent_tasks/task-5-worker-embeddings.md``):

* The default provider is OpenAI ``text-embedding-3-small`` with a 1536-d
  output. The model id and dimension are **hard-coded** in v1 — not
  parameterised.
* The helper never raises. On any failure (timeout, 5xx after retry,
  malformed response, missing credentials) it returns ``None`` and the
  caller writes the memory row with ``content_vec = NULL``.
* 5-second timeout, up to one retry on connection errors or 5xx responses.
* No input text is ever logged. Log lines carry only structural metadata
  (``tokens``, ``latency_ms``, ``cost_microdollars`` on success;
  ``error_class`` / ``error_message_short`` on failure).
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# Hard-coded platform defaults — v1 does not parameterise these.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSION = 1536

# The embedding provider identifier in the ``provider_keys`` table. OpenAI's
# embedding endpoint shares credentials with the chat provider today, so we
# reuse the existing ``openai`` row; if a future provider needs a different
# credential row the constant stays a single-source-of-truth.
EMBEDDING_PROVIDER_ID = "openai"
EMBEDDING_API_URL = "https://api.openai.com/v1/embeddings"

# OpenAI published rate-card price for text-embedding-3-small is $0.02 per
# 1M input tokens, i.e. 20_000 microdollars per million tokens. This constant
# is only used when the ``models`` table does not yet contain a row for the
# embedding model. Embedding cost is zero-rated in v1 (see design doc
# "Embeddings → Cost accounting") — the value is recorded only for
# observability parity with the chat-model ledger.
FALLBACK_EMBEDDING_INPUT_MICRODOLLARS_PER_MTOK = 20_000

# Per-call HTTP timeout (seconds). The design doc calls out 5 seconds.
EMBEDDING_HTTP_TIMEOUT_SECONDS = 5.0

# Retry budget: original attempt plus up to this many retries. "Up to 1
# retry" per the task spec.
EMBEDDING_MAX_RETRIES = 1


@dataclass(frozen=True)
class EmbeddingResult:
    """Return value of :func:`compute_embedding`.

    The vector is delivered alongside the provider-reported ``prompt_tokens``
    and a cost in microdollars computed from the pricing helper below. The
    caller (Task 6 / Task 8) is responsible for writing cost rows into
    ``agent_cost_ledger``; this module does not touch the ledger.
    """

    vector: list[float]
    tokens: int
    cost_microdollars: int


ApiKeyLoader = Callable[[], Awaitable[Optional[str]]]


async def _default_api_key_loader(pool: asyncpg.Pool | None) -> Optional[str]:
    """Read the embedding-provider API key from ``provider_keys``.

    The worker's chat-model provider reads credentials with the same query
    (see ``executor/providers.create_llm``); mirroring that pattern keeps
    the two providers behind one credential store. Returns ``None`` when no
    row exists so callers can decide what to do (log and give up / defer
    vector).
    """
    if pool is None:
        # Fall back to an environment variable so local unit tests and
        # development runs that never hit Postgres still work. This is the
        # same escape hatch the chat-model provider would use if the pool
        # were absent.
        env_key = os.environ.get("OPENAI_API_KEY")
        return env_key.strip() if env_key else None

    async with pool.acquire() as conn:
        api_key = await conn.fetchval(
            "SELECT api_key FROM provider_keys WHERE provider_id = $1",
            EMBEDDING_PROVIDER_ID,
        )
    if api_key:
        return str(api_key).strip()

    # Final fallback: process env. Matches the chat-path escape hatch used
    # in local development.
    env_key = os.environ.get("OPENAI_API_KEY")
    return env_key.strip() if env_key else None


def _approx_token_count(text: str) -> int:
    """Conservative token approximation when the provider response omits
    ``usage.prompt_tokens``. Four-characters-per-token is the rule-of-thumb
    published by OpenAI for English text; good enough for the ledger, and
    callers can still act on the value as if it came from the provider.
    """
    return max(1, math.ceil(len(text) / 4))


def _compute_cost_microdollars(
    tokens: int, *, price_per_mtok: int = FALLBACK_EMBEDDING_INPUT_MICRODOLLARS_PER_MTOK
) -> int:
    """Integer microdollars for a given token count. Embedding cost is
    zero-rated in v1 so the number is advisory only, but we still compute
    and return it so callers can log it consistently."""
    if tokens <= 0:
        return 0
    return (tokens * price_per_mtok) // 1_000_000


def _validate_vector(raw: object) -> list[float] | None:
    """Return a list of floats iff ``raw`` is a list of numbers with the
    expected dimension; otherwise ``None``.
    """
    if not isinstance(raw, list):
        return None
    if len(raw) != DEFAULT_EMBEDDING_DIMENSION:
        return None
    out: list[float] = []
    for value in raw:
        if isinstance(value, bool):  # ``bool`` is a subclass of ``int``.
            return None
        if not isinstance(value, (int, float)):
            return None
        out.append(float(value))
    return out


async def _post_embedding(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    text: str,
) -> httpx.Response:
    return await client.post(
        EMBEDDING_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEFAULT_EMBEDDING_MODEL,
            "input": text,
        },
        timeout=EMBEDDING_HTTP_TIMEOUT_SECONDS,
    )


def _log_failure(error_class: str, error_message_short: str) -> None:
    logger.warning(
        "memory.embedding.failed error_class=%s error_message=%s",
        error_class,
        error_message_short,
    )


def _log_success(tokens: int, latency_ms: int, cost_microdollars: int) -> None:
    logger.info(
        "memory.embedding.succeeded tokens=%d latency_ms=%d cost_microdollars=%d",
        tokens,
        latency_ms,
        cost_microdollars,
    )


async def compute_embedding(
    text: str,
    *,
    pool: asyncpg.Pool | None = None,
    client: httpx.AsyncClient | None = None,
    api_key_loader: ApiKeyLoader | None = None,
) -> EmbeddingResult | None:
    """Compute an embedding for ``text`` using the platform-default provider.

    Returns an :class:`EmbeddingResult` on success; ``None`` on any failure
    (credential missing, provider unreachable, 5xx after one retry,
    malformed response, wrong dimension). **Never raises.**

    Parameters
    ----------
    text:
        The text to embed. Sent verbatim to the provider — callers must do
        their own normalisation. Never logged.
    pool:
        asyncpg pool used to read ``provider_keys``. Only required when
        ``api_key_loader`` is ``None`` and the default loader is used.
    client:
        Optional pre-built ``httpx.AsyncClient``. Tests inject a mock
        transport here. In production the helper creates a client per call
        — the worker process is long-lived but embeddings are infrequent
        relative to chat traffic so the connection-reuse benefit is small
        and the simplicity of a per-call client is worth more.
    api_key_loader:
        Optional async callable returning the api key. Tests use this hook
        to avoid touching Postgres.
    """

    started = time.monotonic()

    loader: ApiKeyLoader
    if api_key_loader is not None:
        loader = api_key_loader
    else:
        async def _default_loader() -> Optional[str]:
            return await _default_api_key_loader(pool)

        loader = _default_loader

    try:
        api_key = await loader()
    except Exception as exc:  # pragma: no cover - credential lookup failure is rare
        _log_failure(type(exc).__name__, _short(str(exc)))
        return None

    if not api_key:
        _log_failure("MissingCredential", "no api key for embedding provider")
        return None

    owns_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=EMBEDDING_HTTP_TIMEOUT_SECONDS)
        owns_client = True

    try:
        response: httpx.Response | None = None
        last_error_class: str | None = None
        last_error_message: str | None = None

        for attempt in range(EMBEDDING_MAX_RETRIES + 1):
            try:
                response = await _post_embedding(client, api_key=api_key, text=text)
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.WriteError,
            ) as exc:
                last_error_class = type(exc).__name__
                last_error_message = _short(str(exc))
                response = None
                # Retryable transport error — loop continues for one retry.
                continue
            except httpx.HTTPError as exc:
                last_error_class = type(exc).__name__
                last_error_message = _short(str(exc))
                response = None
                # Unexpected httpx failure — treat as non-retryable to avoid
                # thrashing when the underlying issue (e.g. SSL cert) won't
                # resolve on a second attempt.
                break

            if response.status_code >= 500:
                last_error_class = f"HTTP{response.status_code}"
                last_error_message = f"upstream {response.status_code}"
                response = None
                continue
            # Any non-5xx response (including 4xx) is terminal. 4xx usually
            # means the credential or request is wrong; retrying won't help.
            break

        if response is None:
            _log_failure(
                last_error_class or "UnknownProviderError",
                last_error_message or "embedding request failed",
            )
            return None

        if response.status_code >= 400:
            # Don't echo the provider's response body at WARNING — it can
            # carry the rate-limit-echoed request payload or provider-
            # specific error text that we don't want to forward to ops
            # log sinks. Log a fixed message at WARNING; demote the body
            # to DEBUG for deliberate diagnostics.
            _log_failure(
                f"HTTP{response.status_code}",
                "embedding provider rejected request",
            )
            logger.debug(
                "memory.embedding.failed_body status=%d body=%s",
                response.status_code,
                _short(response.text),
            )
            return None

        try:
            payload = response.json()
        except ValueError as exc:
            _log_failure("InvalidJson", _short(str(exc)))
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            _log_failure("MalformedResponse", "missing data array")
            return None

        first = data[0]
        if not isinstance(first, dict):
            _log_failure("MalformedResponse", "data[0] not an object")
            return None

        vector = _validate_vector(first.get("embedding"))
        if vector is None:
            _log_failure(
                "MalformedResponse",
                "embedding missing or wrong dimension",
            )
            return None

        usage = payload.get("usage") if isinstance(payload, dict) else None
        tokens: int
        if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
            tokens = int(usage["prompt_tokens"])
        else:
            tokens = _approx_token_count(text)

        cost_microdollars = _compute_cost_microdollars(tokens)
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_success(tokens=tokens, latency_ms=latency_ms, cost_microdollars=cost_microdollars)

        return EmbeddingResult(
            vector=vector,
            tokens=tokens,
            cost_microdollars=cost_microdollars,
        )
    finally:
        if owns_client:
            await client.aclose()


def _short(message: str, limit: int = 200) -> str:
    """Truncate an error message. Error payloads can be large (HTML bodies,
    stack traces) and we do not want to spam the log.
    """
    cleaned = " ".join(message.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "..."
