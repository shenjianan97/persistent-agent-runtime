"""Provider-agnostic prompt caching strategy.

Prompt caching mechanisms differ per provider — Anthropic wants explicit
``cache_control`` blocks on content, Bedrock Converse uses the same shape via
LangChain's converse translator, OpenAI caches automatically and reports
cached tokens in ``prompt_tokens_details``, and newer providers will bring
their own. To keep the agent loop provider-neutral we funnel all of that
through a :class:`PromptCacheStrategy` with two responsibilities:

1. :meth:`apply_cache_markers` — given the final projection the agent is
   about to send, annotate it with provider-specific cache hints. Must be
   pure (return a new list) so callers can log/replay the pre- and
   post-marker shapes without confusion.
2. :meth:`extract_token_usage` — parse the provider's response metadata into
   a common :class:`TokenUsage` with explicit cache creation / cache read
   counters. Downstream cost accounting and Langfuse emission operate on the
   common shape and never look at provider-specific keys directly.

Adding a new provider is a one-file change in this package plus a registry
entry at the bottom — ``executor.graph`` does not grow provider branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import BaseMessage


@dataclass(frozen=True)
class TokenUsage:
    """Provider-neutral token counts.

    ``cache_creation_input_tokens`` is Anthropic-specific (tokens written to
    the cache on a cache-miss turn). ``cache_read_input_tokens`` is the
    universal cache-hit counter — Anthropic reports it natively, Bedrock
    returns the same name via Converse, OpenAI surfaces it as
    ``prompt_tokens_details.cached_tokens``.

    ``input_tokens`` excludes cache creation + cache read tokens; sum all
    three to recover the provider's total prompt token count.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


class PromptCacheStrategy(Protocol):
    """Strategy contract — see module docstring."""

    provider: str

    def apply_cache_markers(
        self, messages: list[BaseMessage]
    ) -> list[BaseMessage]:
        """Return a new message list with provider-specific cache hints."""
        ...

    def extract_token_usage(self, response_metadata: dict) -> TokenUsage:
        """Parse the LLM's response metadata into a :class:`TokenUsage`."""
        ...

    def supports_caching(self, model: str) -> bool:
        """Return True when *model* accepts this strategy's cache markers.

        Primarily matters for Bedrock, which hosts many third-party model
        families (Claude, Nova, GLM, Llama, Mistral, Cohere) — only Claude
        and Nova accept ``cachePoint`` blocks; everything else returns
        ``AccessDeniedException``. For Anthropic-native and OpenAI the
        answer is effectively always True.
        """
        ...
