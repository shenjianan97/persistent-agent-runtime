"""Unit tests for ``executor/memory_graph.py`` (Phase 2 Track 5 Task 6).

Covers the ``memory_write`` LangGraph node and its fallback helper:

- ``RuntimeState`` carries ``observations`` and ``pending_memory`` fields,
  with ``operator.add`` as the reducer on ``observations`` — appending a single
  note via ``Command(update=...)`` preserves prior notes.
- ``memory_write_node`` on the happy path calls the injected summarizer
  callable and ``compute_embedding``, returning a ``Command`` that populates
  ``pending_memory`` with title / summary / embedding / observations / tags.
- Summarizer exhaustion (repeated raises) → template fallback with
  ``summarizer_model_id='template:fallback'``.
- Embedding returning ``None`` → ``pending_memory['content_vec']`` is ``None``.
- ``tags`` is always an empty list in v1.
- The fallback helper produces a stable shape independently.
- ``effective_memory_decision`` truth-table (Task 12).

The node is designed to take injected callables (summarizer / embedding / cost
recorder) so unit tests never touch a provider, the network, or Postgres.
"""

from __future__ import annotations

import operator
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from executor.compaction.state import RuntimeState
from executor.embeddings import EmbeddingResult
from executor.memory_graph import (
    MemoryDecision,
    PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    build_pending_memory_template_fallback,
    effective_memory_decision,
    memory_write_node,
)


class TestEffectiveMemoryDecisionTruthTable:
    """Task 12 — full (enabled ∈ {True, False}) × (mode ∈ {always,
    agent_decides, skip}) truth table. Assertions cover both
    ``stack_enabled`` and ``auto_write`` so regressions in either column
    surface here first.
    """

    def test_enabled_always(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": True}},
            memory_mode="always",
        )
        assert d == MemoryDecision(stack_enabled=True, auto_write=True)

    def test_enabled_agent_decides(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": True}},
            memory_mode="agent_decides",
        )
        assert d == MemoryDecision(stack_enabled=True, auto_write=False)

    def test_enabled_skip(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": True}},
            memory_mode="skip",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_disabled_always(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": False}},
            memory_mode="always",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_disabled_agent_decides(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": False}},
            memory_mode="agent_decides",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_disabled_skip(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": False}},
            memory_mode="skip",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_agent_without_memory_section(self) -> None:
        d = effective_memory_decision(
            agent_config={},
            memory_mode="always",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_memory_section_but_no_enabled_key(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"max_entries": 50}},
            memory_mode="always",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_unrecognised_mode_collapses_to_disabled_stack(self) -> None:
        """Guards against a mis-serialized payload silently writing a memory
        the customer didn't ask for — unknown modes must behave like ``skip``.
        """
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": True}},
            memory_mode="banana",
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)

    def test_non_string_mode_collapses_to_disabled_stack(self) -> None:
        d = effective_memory_decision(
            agent_config={"memory": {"enabled": True}},
            memory_mode=None,  # type: ignore[arg-type]
        )
        assert d == MemoryDecision(stack_enabled=False, auto_write=False)


class TestTemplateFallback:
    def test_title_uses_first_80_chars_of_input(self) -> None:
        long_input = "a" * 200
        pm = build_pending_memory_template_fallback(
            task_input=long_input,
            final_output="final",
            observations=["obs-1"],
        )
        # "Completed: " prefix + 80 chars = total 91 chars
        assert pm["title"].startswith("Completed: ")
        # Title max ~91 chars; specifically bounded by 80 chars of input slice
        input_slice = pm["title"][len("Completed: "):]
        assert len(input_slice) == 80

    def test_summary_truncates_final_output_and_flags_unavailable(self) -> None:
        pm = build_pending_memory_template_fallback(
            task_input="short input",
            final_output="x" * 5000,
            observations=[],
        )
        assert "summary generation unavailable" in pm["summary"]
        # ~1KB cap on the leading final-output portion.
        assert len(pm["summary"]) < 5000

    def test_outcome_succeeded(self) -> None:
        pm = build_pending_memory_template_fallback(
            task_input="i", final_output="o", observations=[]
        )
        assert pm["outcome"] == "succeeded"

    def test_summarizer_model_id_is_template_fallback(self) -> None:
        pm = build_pending_memory_template_fallback(
            task_input="i", final_output="o", observations=[]
        )
        assert pm["summarizer_model_id"] == "template:fallback"

    def test_tags_is_empty_list(self) -> None:
        pm = build_pending_memory_template_fallback(
            task_input="i", final_output="o", observations=[]
        )
        assert pm["tags"] == []

    def test_observations_snapshot_is_verbatim(self) -> None:
        notes = ["n1", "n2", "n3"]
        pm = build_pending_memory_template_fallback(
            task_input="i", final_output="o", observations=notes
        )
        assert pm["observations_snapshot"] == notes

    def test_handles_none_final_output(self) -> None:
        pm = build_pending_memory_template_fallback(
            task_input="i", final_output=None, observations=[]
        )
        # Must not crash and must still produce a string summary.
        assert isinstance(pm["summary"], str)
        assert pm["summary"]


class TestRuntimeState:
    """Track 7 Task 2 — exercises the ``RuntimeState`` unified schema
    (``executor.compaction.state``).  The AC-5 manifest in
    ``test_track5_ac_mapping.py`` references this class.
    """

    def test_observations_reducer_is_operator_add(self) -> None:
        # LangGraph inspects the annotated metadata on the TypedDict key.
        # We verify the ``observations`` field was wired with ``operator.add``
        # so that Task 7's ``memory_note`` tool can append via
        # ``Command(update={"observations": [note]})``.
        from typing import get_type_hints

        hints = get_type_hints(RuntimeState, include_extras=True)
        obs_annotation = hints["observations"]
        # ``Annotated[list[str], operator.add]`` — the metadata tuple holds
        # operator.add as the reducer.
        assert getattr(obs_annotation, "__metadata__", ()) == (operator.add,)

    def test_memory_opt_in_field_declared_without_reducer(self) -> None:
        """Task 12 — ``memory_opt_in`` is a bool field with no reducer, so
        the ``save_memory`` tool's ``Command(update={"memory_opt_in": True})``
        overwrites via last-write-wins and the per-run reset in
        :meth:`GraphExecutor.execute_task` can cleanly seed ``False`` on
        every new run.
        """
        from typing import get_type_hints

        hints = get_type_hints(RuntimeState, include_extras=True)
        assert "memory_opt_in" in hints
        opt_in_annotation = hints["memory_opt_in"]
        # Bare ``bool`` — no ``Annotated[..., reducer]`` metadata attached.
        assert getattr(opt_in_annotation, "__metadata__", None) is None
        assert opt_in_annotation is bool


class TestMemoryWriteNodeHappyPath:
    @pytest.mark.asyncio
    async def test_happy_path_populates_pending_memory(self) -> None:
        summarizer_calls = []

        async def fake_summarizer(*, system: str, user: str, model_id: str):
            summarizer_calls.append((model_id, system, user))
            return SimpleNamespace(
                title="Shipped login fix",
                summary="Diagnosed auth token rot, patched service",
                model_id=model_id,
                tokens_in=120,
                tokens_out=30,
                cost_microdollars=1500,
            )

        async def fake_embedding(text: str) -> EmbeddingResult | None:
            return EmbeddingResult(
                vector=[0.1] * 1536,
                tokens=77,
                cost_microdollars=2,
            )

        state = {
            "messages": [
                HumanMessage(content="What was the fix?"),
                AIMessage(content="Final answer: rotated tokens"),
            ],
            "observations": ["noted: tokens expired", "rotated creds"],
            "pending_memory": None,
        }

        result = await memory_write_node(
            state,
            task_input="Investigate auth failure",
            summarizer_model_id="claude-haiku-4-5",
            summarizer_callable=fake_summarizer,
            embedding_callable=fake_embedding,
        )

        assert isinstance(result, Command)
        assert result.update is not None
        pm = result.update["pending_memory"]

        assert pm["title"] == "Shipped login fix"
        assert pm["summary"] == "Diagnosed auth token rot, patched service"
        assert pm["outcome"] == "succeeded"
        assert pm["summarizer_model_id"] == "claude-haiku-4-5"
        assert pm["observations_snapshot"] == [
            "noted: tokens expired",
            "rotated creds",
        ]
        assert pm["tags"] == []
        assert pm["content_vec"] == [0.1] * 1536
        # Observability fields used by the caller for ledger attribution.
        assert pm["summarizer_tokens_in"] == 120
        assert pm["summarizer_tokens_out"] == 30
        assert pm["summarizer_cost_microdollars"] == 1500
        assert pm["embedding_tokens"] == 77
        assert pm["embedding_cost_microdollars"] == 2
        # Summarizer was invoked with the configured model.
        assert summarizer_calls and summarizer_calls[0][0] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_uses_platform_default_when_agent_has_no_summarizer_model(
        self,
    ) -> None:
        captured = {}

        async def fake_summarizer(*, system: str, user: str, model_id: str):
            captured["model_id"] = model_id
            return SimpleNamespace(
                title="T", summary="S", model_id=model_id,
                tokens_in=1, tokens_out=1, cost_microdollars=0,
            )

        async def fake_embedding(text: str) -> EmbeddingResult | None:
            return EmbeddingResult(vector=[0.0] * 1536, tokens=1, cost_microdollars=0)

        state = {
            "messages": [AIMessage(content="done")],
            "observations": [],
            "pending_memory": None,
        }

        await memory_write_node(
            state,
            task_input="anything",
            summarizer_model_id=None,
            summarizer_callable=fake_summarizer,
            embedding_callable=fake_embedding,
        )

        assert captured["model_id"] == PLATFORM_DEFAULT_SUMMARIZER_MODEL

    @pytest.mark.asyncio
    async def test_embedding_text_includes_title_summary_observations_tags(
        self,
    ) -> None:
        embed_text_seen = []

        async def fake_summarizer(*, system: str, user: str, model_id: str):
            return SimpleNamespace(
                title="TTT", summary="SSS", model_id=model_id,
                tokens_in=0, tokens_out=0, cost_microdollars=0,
            )

        async def fake_embedding(text: str) -> EmbeddingResult | None:
            embed_text_seen.append(text)
            return EmbeddingResult(
                vector=[0.0] * 1536, tokens=0, cost_microdollars=0
            )

        state = {
            "messages": [AIMessage(content="done")],
            "observations": ["obs-A", "obs-B"],
            "pending_memory": None,
        }

        await memory_write_node(
            state,
            task_input="input",
            summarizer_model_id="claude-haiku-4-5",
            summarizer_callable=fake_summarizer,
            embedding_callable=fake_embedding,
        )

        assert embed_text_seen, "embedding callable must be invoked"
        text = embed_text_seen[0]
        assert "TTT" in text
        assert "SSS" in text
        assert "obs-A" in text
        assert "obs-B" in text


class TestMemoryWriteNodeSummarizerOutage:
    @pytest.mark.asyncio
    async def test_summarizer_raises_writes_template_fallback(self) -> None:
        async def raising_summarizer(*, system: str, user: str, model_id: str):
            raise RuntimeError("provider down")

        async def fake_embedding(text: str) -> EmbeddingResult | None:
            return EmbeddingResult(
                vector=[0.2] * 1536, tokens=3, cost_microdollars=1
            )

        state = {
            "messages": [
                HumanMessage(content="Clean up old data"),
                AIMessage(content="Cleaned up 42 rows"),
            ],
            "observations": ["step-a", "step-b"],
            "pending_memory": None,
        }

        result = await memory_write_node(
            state,
            task_input="Clean up old data",
            summarizer_model_id="claude-haiku-4-5",
            summarizer_callable=raising_summarizer,
            embedding_callable=fake_embedding,
        )

        pm = result.update["pending_memory"]
        assert pm["summarizer_model_id"] == "template:fallback"
        assert pm["outcome"] == "succeeded"
        assert "Clean up old data" in pm["title"]
        assert pm["observations_snapshot"] == ["step-a", "step-b"]
        assert pm["content_vec"] == [0.2] * 1536
        assert pm["summarizer_cost_microdollars"] == 0
        assert pm["summarizer_tokens_in"] == 0
        assert pm["summarizer_tokens_out"] == 0


class TestMemoryWriteNodeEmbeddingOutage:
    @pytest.mark.asyncio
    async def test_embedding_returns_none_populates_null_content_vec(
        self,
    ) -> None:
        async def fake_summarizer(*, system: str, user: str, model_id: str):
            return SimpleNamespace(
                title="T", summary="S", model_id=model_id,
                tokens_in=5, tokens_out=5, cost_microdollars=20,
            )

        async def fake_embedding_none(text: str) -> EmbeddingResult | None:
            return None

        state = {
            "messages": [AIMessage(content="final")],
            "observations": [],
            "pending_memory": None,
        }

        result = await memory_write_node(
            state,
            task_input="task",
            summarizer_model_id="claude-haiku-4-5",
            summarizer_callable=fake_summarizer,
            embedding_callable=fake_embedding_none,
        )

        pm = result.update["pending_memory"]
        assert pm["content_vec"] is None
        assert pm["embedding_tokens"] == 0
        assert pm["embedding_cost_microdollars"] == 0

    @pytest.mark.asyncio
    async def test_both_summarizer_and_embedding_fail(self) -> None:
        async def raising_summarizer(*, system: str, user: str, model_id: str):
            raise RuntimeError("summarizer down")

        async def fake_embedding_none(text: str) -> EmbeddingResult | None:
            return None

        state = {
            "messages": [AIMessage(content="final")],
            "observations": ["a"],
            "pending_memory": None,
        }

        result = await memory_write_node(
            state,
            task_input="task input",
            summarizer_model_id="claude-haiku-4-5",
            summarizer_callable=raising_summarizer,
            embedding_callable=fake_embedding_none,
        )

        pm = result.update["pending_memory"]
        assert pm["summarizer_model_id"] == "template:fallback"
        assert pm["content_vec"] is None
        assert pm["outcome"] == "succeeded"  # task itself succeeded.


class TestPlatformDefaultSummarizerModel:
    def test_env_override_when_set(self, monkeypatch) -> None:
        # The env var is resolved at module import time. Re-import to verify
        # the precedence rule.
        monkeypatch.setenv("MEMORY_DEFAULT_SUMMARIZER_MODEL", "custom-haiku")
        import importlib

        import executor.memory_graph as memory_graph

        importlib.reload(memory_graph)
        try:
            assert memory_graph.PLATFORM_DEFAULT_SUMMARIZER_MODEL == "custom-haiku"
        finally:
            # Restore module state for subsequent tests.
            monkeypatch.delenv("MEMORY_DEFAULT_SUMMARIZER_MODEL", raising=False)
            importlib.reload(memory_graph)

    def test_compiled_in_default_when_env_absent(self) -> None:
        # Either the env var is already set (the reload test above) or the
        # module stays on the compiled-in fallback. Both literals are
        # documented in the task spec — we assert the fallback literal is
        # cheap Haiku-class by prefix.
        assert PLATFORM_DEFAULT_SUMMARIZER_MODEL.startswith("claude-haiku")
