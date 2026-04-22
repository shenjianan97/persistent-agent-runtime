"""No-op strategy for providers with no prompt-caching support."""

from __future__ import annotations

from langchain_core.messages import BaseMessage

from executor.prompt_cache.strategy import PromptCacheStrategy, TokenUsage


class NoopPromptCacheStrategy(PromptCacheStrategy):
    provider = "noop"

    def supports_caching(self, model: str) -> bool:
        return False

    def apply_cache_markers(
        self, messages: list[BaseMessage]
    ) -> list[BaseMessage]:
        return list(messages)

    def extract_token_usage(self, response_metadata: dict) -> TokenUsage:
        usage = (
            response_metadata.get("usage")
            or response_metadata.get("token_usage")
            or response_metadata.get("usage_metadata")
            or {}
        )
        input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_t = (
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        return TokenUsage(
            input_tokens=int(input_t),
            output_tokens=int(output_t),
        )
