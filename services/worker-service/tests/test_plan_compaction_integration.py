"""Planning Primitive — Task P5: plan survives compaction (worker integration).

Drives the REAL graph built by ``GraphExecutor._build_graph`` (real
``agent_node`` → ``compaction_pre_model_hook`` → ``inject_plan_block`` →
``apply_cache_markers`` pipeline, real ``ToolNode`` executing the real
``plan_write`` tool) with a scripted fake LLM, an ``InMemorySaver``
checkpointer, and a small ``model_context_window`` so Tier-3 summarisation
fires deterministically. No Postgres / S3 / network — pure-worker, runs
under the pinned venv with no infra.

What this proves (the P5 manifest, behavior 1):

* The stub model writes a plan via ``plan_write`` on its first turn, then
  emits enough filler turns that the projection crosses
  ``COMPACTION_TRIGGER_FRACTION * model_context_window`` and Tier-3
  summarisation fires (cross-checked three ways: the ``Tier3FiredEvent``
  handed to ``_emit_compaction_task_events``, the compaction-tagged summary
  ``SystemMessage`` in the LLM-bound projection, and the final graph state's
  ``tier3_firings_count`` / ``summarized_through_turn_index`` / ``summary``).
* On a turn strictly AFTER the firing, the LLM-bound projection (the exact
  list passed to ``llm.ainvoke`` — post cache-markers) still contains the
  injected plan block (``is_plan_block``) with every plan title intact and
  byte-identical to ``render_plan_block(PLAN)``.
* History that fell into the summarised middle is GONE from that projection
  (masked into the summary region) — the plan block survived a transform
  that demonstrably destroyed its neighbours.
* The durable journal (``state["messages"]``) NEVER contains the plan block
  — injection is projection-only.

Terminology note — "Tier-1 masking": the legacy standalone Tier-1
tool-result-clearing pass (``compute_thresholds`` /
``TIER_1_TRIGGER_FRACTION`` in ``executor/compaction/thresholds.py``) was
retired with Track 7 Follow-up Task 3's ``pre_model_hook`` rewrite
(``tests/test_pre_model_hook.py::test_compact_for_llm_symbol_gone`` pins the
old entry point's removal; ``compute_thresholds`` has no remaining callers
in ``executor/``). Its surviving equivalent is the hook's three-region
projection: journal history outside the keep window is masked from the
verbatim tail into the middle region and, on a Tier-3 firing, replaced by
the summary. This test asserts the plan block survives exactly that
masking+summarisation transform — the strongest "Tier 1/3" claim the
current architecture supports.

Plan-block size measurement (track obligation — Planning Primitive
"Named follow-ups" #2): see
``test_plan_block_size_measurement_for_max_plan`` at the bottom, which
renders the worst-case 50-item × 200-char-title plan and reports its
rendered size in chars and estimated tokens.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from core.config import WorkerConfig
from executor.compaction.defaults import COMPACTION_TRIGGER_FRACTION
from executor.compaction.pre_model_hook import Tier3FiredEvent
from executor.graph import GraphExecutor
from executor.plan_injection import is_plan_block, render_plan_block
from tools.plan_tools import PLAN_MAX_ITEMS, PLAN_MAX_TITLE_CHARS


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

#: The plan the stub agent writes on turn 1 and must still see after Tier-3.
PLAN: list[dict] = [
    {"id": "p1", "title": "Survey the source corpus", "status": "completed"},
    {"id": "p2", "title": "Cluster recurring findings", "status": "in_progress"},
    {"id": "p3", "title": "Draft the final report", "status": "pending"},
]

#: Small main-model window so the trigger fires after a handful of turns.
#: Trigger = 0.85 × 8_000 = 6_800 est tokens (anthropic heuristic:
#: serialized_bytes // 3 → ~20.4 KB of serialized history).
MODEL_CONTEXT_WINDOW: int = 8_000

#: Per-turn filler size. ~2_400 chars ≈ 800 est tokens — comfortably below
#: the 1_200-token gap between the trigger (0.85·W) and the hard floor (W),
#: so history growth can never leap from below-trigger straight past the
#: context-exceeded dead-letter path.
FILLER_CHARS: int = 2_400

#: Safety valve: the fake LLM force-finishes after this many turns even if
#: Tier-3 never fired (the test then fails its firing assertions with full
#: diagnostics instead of spinning forever).
MAX_TURNS: int = 40


def _has_compaction_summary(messages: list[BaseMessage]) -> bool:
    """True when the projection carries the Tier-3 summary region."""
    return any(
        isinstance(m, SystemMessage)
        and (getattr(m, "additional_kwargs", None) or {}).get("compaction")
        for m in messages
    )


class _RecordingScriptedLLM:
    """Fake chat model: records every LLM-bound projection, scripts turns.

    Script:

    1. First call → ``plan_write`` tool call carrying :data:`PLAN`.
    2. Until the Tier-3 summary region appears in the incoming projection →
       filler turns (large unique content + a ``plan_write`` rewrite of the
       same plan, which keeps the agent↔tools loop running without needing
       network-backed tools).
    3. First call where the summary IS present (the firing turn itself) →
       one more filler turn, so the run continues past the firing.
    4. Next call (a turn strictly AFTER the firing) → final answer with no
       tool calls; the graph routes to END.
    """

    def __init__(self) -> None:
        self.projections: list[list[BaseMessage]] = []
        self.turn: int = 0
        self.post_summary_turns: int = 0

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - LangChain seam
        return self

    async def ainvoke(self, messages, config=None, **kwargs):  # noqa: ANN001
        self.projections.append(list(messages))
        self.turn += 1
        usage = {
            "usage": {"input_tokens": 50, "output_tokens": 10},
        }

        def _plan_call(turn: int, content: str = "") -> AIMessage:
            return AIMessage(
                content=content,
                tool_calls=[
                    {
                        "name": "plan_write",
                        "args": {"items": PLAN},
                        "id": f"tc-plan-{turn}",
                    }
                ],
                response_metadata=usage,
            )

        if self.turn == 1:
            # Turn 1: write the plan before any compaction can fire.
            return _plan_call(self.turn)

        summary_present = _has_compaction_summary(messages)
        if (summary_present and self.post_summary_turns >= 1) or (
            self.turn > MAX_TURNS
        ):
            # A turn strictly after the Tier-3 firing → finish.
            return AIMessage(content="RESEARCH COMPLETE", response_metadata=usage)
        if summary_present:
            self.post_summary_turns += 1

        filler = f"FILLER-TURN-{self.turn} " + ("x" * FILLER_CHARS)
        return _plan_call(self.turn, content=filler)


# ---------------------------------------------------------------------------
# DB-free pool stand-in — only the summariser's cost-ledger INSERT touches it
# (``_PoolBackedCostLedger.insert`` → ``pool.acquire()`` → ``conn.execute``).
# ---------------------------------------------------------------------------


class _FakeAcquire:
    def __init__(self, conn) -> None:  # noqa: ANN001
        self._conn = conn

    async def __aenter__(self):  # noqa: ANN204
        return self._conn

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class _FakePool:
    def __init__(self) -> None:
        self.conn = MagicMock()
        self.conn.execute = AsyncMock(return_value="INSERT 0 1")
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.conn.fetchval = AsyncMock(return_value=None)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


def _initial_state(task_input: str) -> dict:
    """Reducer-safe initial RuntimeState — mirrors ``execute_task``'s
    first-execution ``_payload`` seed (executor/graph.py ~:3049), which
    deliberately does NOT seed ``plan`` (the channel is born on the first
    ``plan_write``)."""
    return {
        "messages": [HumanMessage(content=task_input)],
        "observations": [],
        "commit_rationales": [],
        "projected_observations": [],
        "projected_commit_rationales": [],
        "pending_memory": {},
        "memory_opt_in": False,
        "summary": "",
        "summarized_through_turn_index": 0,
        "memory_flush_fired_this_task": False,
        "last_super_step_message_count": 0,
        "tier3_firings_count": 0,
        "tier3_fatal_short_circuited": False,
    }


SUMMARY_TEXT = (
    "SUMMARY-OF-EARLIER-TURNS: the agent generated filler research notes "
    "across several turns; no decisions beyond the recorded plan."
)


AGENT_CONFIG: dict = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "temperature": 0.0,
    "system_prompt": "You are a research agent under integration test.",
    "allowed_tools": ["plan_write"],
    # Memory disabled → no memory_write node, no pre-Tier-3 memory flush —
    # the only compaction transform in play is the hook's masking +
    # summarisation, which is what the plan block must survive.
    "memory": {"enabled": False},
    # Offload disabled → no S3 store constructed, no recall tooling; the
    # tool_node wrapper is a tag-only passthrough.
    "context_management": {"offload_tool_results": False},
}


async def _run_plan_compaction_scenario() -> tuple[
    _RecordingScriptedLLM, dict, AsyncMock
]:
    """Build the real graph, run it to completion, return the evidence."""
    fake_llm = _RecordingScriptedLLM()

    summarizer_llm = AsyncMock()
    summarizer_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=SUMMARY_TEXT,
            response_metadata={
                "usage": {"input_tokens": 1_000, "output_tokens": 40}
            },
        )
    )

    emit_spy = AsyncMock()

    executor = GraphExecutor(
        WorkerConfig(worker_id="plan-compaction-itest", tenant_id="default"),
        pool=_FakePool(),
        s3_client=MagicMock(),
    )

    with patch(
        "executor.providers.create_llm",
        new=AsyncMock(return_value=fake_llm),
    ), patch(
        "executor.compaction.summarizer.init_chat_model",
        return_value=summarizer_llm,
    ), patch(
        "executor.graph._emit_compaction_task_events",
        new=emit_spy,
    ), patch.object(
        GraphExecutor,
        "_get_model_context_window",
        new=AsyncMock(return_value=200_000),  # summariser window (big → single call)
    ), patch.object(
        GraphExecutor,
        "_summarizer_pricing_lookup",
        new=AsyncMock(return_value=(0, 0)),
    ):
        workflow = await executor._build_graph(
            AGENT_CONFIG,
            cancel_event=asyncio.Event(),
            task_id=str(uuid.uuid4()),
            tenant_id="default",
            agent_id="plan-itest-agent",
            model_context_window=MODEL_CONTEXT_WINDOW,
        )
        compiled = workflow.compile(checkpointer=InMemorySaver())
        config = {
            "configurable": {"thread_id": "plan-itest-thread"},
            "recursion_limit": 200,
        }
        final_state = await compiled.ainvoke(
            _initial_state("Run the long research task."), config=config
        )

    return fake_llm, final_state, emit_spy


@pytest.fixture(scope="module")
def scenario() -> tuple[_RecordingScriptedLLM, dict, AsyncMock]:
    """Run the (multi-turn) graph once; every test asserts against it."""
    return asyncio.run(_run_plan_compaction_scenario())


# ---------------------------------------------------------------------------
# Cross-check: Tier-3 compaction actually fired
# ---------------------------------------------------------------------------


class TestCompactionActuallyFired:
    def test_tier3_fired_event_emitted(self, scenario) -> None:
        """``agent_node`` handed a ``Tier3FiredEvent`` to the task-events
        emitter — the same signal that backs the worker-log
        ``compaction.tier3_fired`` line and the Console marker."""
        _, _, emit_spy = scenario
        fired = [
            ev
            for call in emit_spy.await_args_list
            for ev in call.kwargs.get("events", [])
            if isinstance(ev, Tier3FiredEvent)
        ]
        assert fired, (
            "Tier-3 summarisation never fired — the scenario did not cross "
            f"the {COMPACTION_TRIGGER_FRACTION} × {MODEL_CONTEXT_WINDOW} "
            "trigger. Check FILLER_CHARS / MAX_TURNS sizing."
        )

    def test_final_state_records_the_firing(self, scenario) -> None:
        _, final_state, _ = scenario
        assert final_state["tier3_firings_count"] >= 1
        assert final_state["summarized_through_turn_index"] > 0
        assert final_state["summary"] == SUMMARY_TEXT

    def test_summary_region_reached_the_llm(self, scenario) -> None:
        """The compaction-tagged summary SystemMessage is in the LAST
        LLM-bound projection — the transform was visible to the model."""
        fake_llm, _, _ = scenario
        last = fake_llm.projections[-1]
        summaries = [
            m
            for m in last
            if isinstance(m, SystemMessage)
            and (m.additional_kwargs or {}).get("compaction")
        ]
        assert summaries, "summary region missing from post-firing projection"
        assert any(SUMMARY_TEXT in str(m.content) for m in summaries)


# ---------------------------------------------------------------------------
# The load-bearing claim: the plan block survives the compaction transform
# ---------------------------------------------------------------------------


class TestPlanSurvivesCompaction:
    def test_plan_block_present_after_tier3_with_content_intact(
        self, scenario
    ) -> None:
        """On a turn strictly AFTER Tier-3 fired (the final-answer turn —
        the fake LLM only stops after seeing the summary on a previous
        turn), the LLM-bound projection still contains exactly one plan
        block, byte-identical to the canonical rendering of the plan the
        agent wrote on turn 1."""
        fake_llm, _, _ = scenario
        last = fake_llm.projections[-1]
        blocks = [m for m in last if is_plan_block(m)]
        assert len(blocks) == 1, (
            f"expected exactly one plan block, got {len(blocks)}"
        )
        block = blocks[0]
        assert block.content == render_plan_block(PLAN)
        for item in PLAN:
            assert item["title"] in block.content

    def test_plan_block_is_projection_tail(self, scenario) -> None:
        """The block sits at the projection tail (uncached suffix)."""
        fake_llm, _, _ = scenario
        last = fake_llm.projections[-1]
        assert is_plan_block(last[-1])

    def test_plan_block_present_on_every_post_plan_write_turn(
        self, scenario
    ) -> None:
        """From turn 2 (first turn after the plan landed in state) onward —
        before, during, and after the firing — every projection carries the
        block. Turn 1 (no plan written yet) carries none."""
        fake_llm, _, _ = scenario
        assert not any(is_plan_block(m) for m in fake_llm.projections[0])
        for i, projection in enumerate(fake_llm.projections[1:], start=2):
            assert any(is_plan_block(m) for m in projection), (
                f"plan block missing from projection of turn {i}"
            )

    def test_summarized_history_was_masked_but_plan_survived(
        self, scenario
    ) -> None:
        """The transform the plan survived was real: filler content from an
        early turn (turn 2 — well outside the keep window at firing time)
        is absent from every message of the post-firing projection. Only
        the plan block — rebuilt from the durable channel each turn —
        outlived the masking."""
        fake_llm, final_state, _ = scenario
        last = fake_llm.projections[-1]
        watermark = final_state["summarized_through_turn_index"]
        # The journal still holds the early filler (the journal is durable)…
        journal = final_state["messages"]
        assert any(
            "FILLER-TURN-2" in str(m.content) for m in journal[:watermark]
        ), "fixture drift: turn-2 filler was not inside the summarised window"
        # …but the LLM-bound projection no longer carries it verbatim.
        assert not any("FILLER-TURN-2" in str(m.content) for m in last), (
            "turn-2 filler should have been absorbed into the summary"
        )

    def test_durable_plan_channel_matches_what_was_written(
        self, scenario
    ) -> None:
        _, final_state, _ = scenario
        assert final_state["plan"] == PLAN


# ---------------------------------------------------------------------------
# Journal hygiene: injection is projection-only
# ---------------------------------------------------------------------------


class TestJournalNeverContainsPlanBlock:
    def test_journal_has_no_plan_block(self, scenario) -> None:
        _, final_state, _ = scenario
        journal = final_state["messages"]
        assert journal, "journal unexpectedly empty"
        assert not any(is_plan_block(m) for m in journal), (
            "the durable journal must never contain the injected plan block"
        )

    def test_journal_still_has_plan_write_tool_traffic(self, scenario) -> None:
        """Sanity: the journal recorded the plan_write tool exchange (the
        plan got in via the tool, not via test fiat)."""
        _, final_state, _ = scenario
        journal = final_state["messages"]
        assert any(
            "Plan updated" in str(m.content)
            for m in journal
            if m.type == "tool"
        )


# ---------------------------------------------------------------------------
# Track obligation — plan-block size measurement (Named follow-ups #2)
# ---------------------------------------------------------------------------


def test_plan_block_size_measurement_for_max_plan(capsys) -> None:
    """Measure the injected plan block's rendered size for the worst-case
    plan allowed by P1's caps (50 items × 200-char titles, all
    ``in_progress`` — the longest per-item rendering).

    Titles are natural-language text (not a repeated character) so the
    tokenizer measurement is representative of real plans.

    Measured on 2026-06-06 against the current ``render_plan_block``
    (numbers below are re-verified by this test's assertions on every run):

    * rendered block: **11,233 chars** (preamble + 50 × ~220-char lines)
    * tiktoken ``cl100k_base``: **1,839 tokens**
    * chars/4 heuristic: ~2,808 tokens
    * worker estimator (``estimate_tokens``, anthropic bytes//3): 3,773 tokens

    Verdict for the deferred-decision ledger: the worst-case uncached-suffix
    cost of the plan block is ≈1.8K real tokens (≈3.8K by the conservative
    in-worker estimator) — under 1% of a 200K-token window per turn, re-sent
    (never cached) on every turn while a max-size plan exists. Typical
    plans (≤10 items, ≤80-char titles) are an order of magnitude smaller
    (≈100–400 tokens). The asserted ceilings below freeze the rendering
    contract: a format change that inflates the block past them fails this
    test and forces a fresh look at the ledger row.
    """
    title_words = (
        "verify the deployment pipeline stages and update the integration "
        "documentation with rollout notes for every affected service team "
    )
    full_title = (title_words * 3)[:PLAN_MAX_TITLE_CHARS]
    assert len(full_title) == PLAN_MAX_TITLE_CHARS
    max_plan = [
        {"id": f"i{n}", "title": full_title, "status": "in_progress"}
        for n in range(PLAN_MAX_ITEMS)
    ]
    block = render_plan_block(max_plan)
    chars = len(block)
    est_tokens_chars4 = chars // 4

    tiktoken_tokens: int | None = None
    try:
        import tiktoken

        tiktoken_tokens = len(tiktoken.get_encoding("cl100k_base").encode(block))
    except Exception:
        pass  # tokenizer optional — chars + heuristic still reported

    from executor.compaction.tokens import estimate_tokens
    from executor.plan_injection import make_plan_block_message

    worker_est = estimate_tokens([make_plan_block_message(max_plan)], "anthropic")

    print(
        "plan-block size (50 items x 200-char titles): "
        f"{chars} chars; chars/4≈{est_tokens_chars4} tokens; "
        f"tiktoken(cl100k_base)={tiktoken_tokens} tokens; "
        f"worker estimate_tokens(anthropic)={worker_est} tokens"
    )

    # Freeze the contract: worst-case block stays bounded. 50 items ×
    # (200-char title + ~20 chars of checkbox/status markers) + preamble.
    assert chars < 11_500, f"plan block grew past the measured ceiling: {chars}"
    if tiktoken_tokens is not None:
        assert tiktoken_tokens < 3_000, (
            f"plan block token cost regressed: {tiktoken_tokens}"
        )
