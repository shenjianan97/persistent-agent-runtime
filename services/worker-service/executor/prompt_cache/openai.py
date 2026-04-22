"""OpenAI prompt-cache strategy.

OpenAI caches prompt prefixes automatically once a request exceeds ~1024
tokens — there is no opt-in marker and no cache-creation concept exposed
to callers, so :meth:`apply_cache_markers` is a no-op. Cache hits surface
through ``usage.prompt_tokens_details.cached_tokens`` on the response and
(in LangChain) through ``usage_metadata.input_token_details.cache_read``.

We populate :attr:`TokenUsage.cache_read_input_tokens` from whichever shape
is present so downstream cost accounting and Langfuse emission can track
OpenAI cache-hit spend separately from uncached input tokens. Cache
*creation* is always zero for OpenAI (the provider absorbs the cost).
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage

from executor.prompt_cache.strategy import PromptCacheStrategy, TokenUsage


class OpenAIPromptCacheStrategy(PromptCacheStrategy):
    provider = "openai"

    def supports_caching(self, model: str) -> bool:
        # OpenAI caches prefixes automatically for all current chat models
        # once the prompt crosses ~1024 tokens — there is no marker to
        # reject, so this is always safe to return True.
        return True

    def apply_cache_markers(
        self, messages: list[BaseMessage]
    ) -> list[BaseMessage]:
        return list(messages)

    def extract_token_usage(self, response_metadata: dict) -> TokenUsage:
        usage_metadata = response_metadata.get("usage_metadata") or {}
        # ``token_usage`` is LangChain's canonical OpenAI shape on
        # response_metadata; ``usage`` is the native OpenAI Python SDK
        # shape (used by any code that bypasses LangChain's translator).
        usage_native = (
            response_metadata.get("token_usage")
            or response_metadata.get("usage")
            or {}
        )

        input_t = int(
            usage_metadata.get("input_tokens")
            or usage_native.get("prompt_tokens")
            or usage_native.get("input_tokens")
            or 0
        )
        output_t = int(
            usage_metadata.get("output_tokens")
            or usage_native.get("completion_tokens")
            or usage_native.get("output_tokens")
            or 0
        )

        details = usage_metadata.get("input_token_details") or {}
        prompt_details = usage_native.get("prompt_tokens_details") or {}
        cache_read = int(
            details.get("cache_read")
            or prompt_details.get("cached_tokens")
            or 0
        )

        # OpenAI reports ``prompt_tokens`` as the total (cached + uncached).
        # Normalise ``input_tokens`` to mean the non-cached portion so the
        # ``TokenUsage`` invariant (``total = input + creation + read``)
        # holds across providers.
        if cache_read and input_t >= cache_read:
            input_t = input_t - cache_read

        return TokenUsage(
            input_tokens=input_t,
            output_tokens=output_t,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cache_read,
        )
