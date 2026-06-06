"""Provider-agnostic prompt caching.

Adding a new provider:

1. Create a new strategy class in its own module under this package that
   implements :class:`PromptCacheStrategy` — at minimum
   :meth:`apply_cache_markers` (can be a no-op if the provider caches
   automatically) and :meth:`extract_token_usage` (returning a
   :class:`TokenUsage` with ``cache_read_input_tokens`` populated from
   whatever shape the provider uses).
2. Register it in :data:`_REGISTRY` below under the same provider id that
   :func:`executor.providers.create_llm` accepts.
3. If the strategy places breakpoints, every breakpoint scan must skip
   messages where :func:`executor.plan_injection.is_plan_block` is True —
   the injected plan block is mutable and must stay in the uncached suffix
   (see the Anthropic strategy's module docstring). Enforced by the
   ``_REGISTRY``-parametrized guard in ``tests/test_plan_injection.py``.

``executor.graph`` obtains a strategy via :func:`get_strategy` and otherwise
stays provider-neutral — no ``if provider == "..."`` branches for cache
handling live in the agent loop.
"""

from __future__ import annotations

from executor.prompt_cache.anthropic import AnthropicPromptCacheStrategy
from executor.prompt_cache.bedrock import BedrockPromptCacheStrategy
from executor.prompt_cache.noop import NoopPromptCacheStrategy
from executor.prompt_cache.openai import OpenAIPromptCacheStrategy
from executor.prompt_cache.strategy import PromptCacheStrategy, TokenUsage

_REGISTRY: dict[str, PromptCacheStrategy] = {
    "anthropic": AnthropicPromptCacheStrategy(),
    "bedrock": BedrockPromptCacheStrategy(),
    "openai": OpenAIPromptCacheStrategy(),
}

_NOOP = NoopPromptCacheStrategy()


def get_strategy(provider: str) -> PromptCacheStrategy:
    """Return the cache strategy for *provider*, or the no-op fallback."""
    return _REGISTRY.get(provider, _NOOP)


__all__ = [
    "PromptCacheStrategy",
    "TokenUsage",
    "get_strategy",
]
