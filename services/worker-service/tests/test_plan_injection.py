"""Planning Primitive — Task P2: post-compaction plan injection tests.

Covers:

* ``render_plan_block`` — the canonical Markdown-checkbox rendering (the v1
  resolution of the design's "Rendering format for injection" open item),
  including byte-identity (cache stability property 1) and neutral framing
  (no compaction/summarization language; one-``in_progress`` guidance lives
  in the preamble).
* ``inject_plan_block`` — the pure projection helper ``agent_node`` calls
  after ``compaction_pre_model_hook`` returns: non-empty plan → tagged
  message appended; empty/absent plan → byte-identical pass-through; never
  mutates its inputs (the durable journal is untouched).
* Cache POSITION (cache stability property 2 — the load-bearing one): the
  REAL provider strategies must leave the plan block outside every cached
  prefix. The plan block changes turn-to-turn in the normal case (a planning
  agent marks items completed), so if it sat inside a marked prefix every
  plan edit would invalidate the conversation cache. The strategies skip
  plan-tagged messages for BOTH breakpoints (stable last-system + sliding
  tail), creating the uncached suffix the block lives in.

Message-type note (documented deviation from the task contract's literal
"SystemMessage" wording): the injected block is a ``HumanMessage``, not a
``SystemMessage``. ``langchain_anthropic`` 1.3.4 ``_format_messages`` raises
``ValueError("Received multiple non-consecutive system messages.")`` for any
system message that appears after a non-system turn (chat_models.py:437-440,
verified by spike against the pinned venv), and the uncached suffix is by
definition after the conversation. This mirrors the Track-7 pre-Tier-3
memory-flush precedent (``executor/compaction/pre_model_hook.py`` — "Must
NOT be a SystemMessage"), which the task spec designates as the placement
discipline to match.
"""

from __future__ import annotations

import copy
import pathlib

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from executor.plan_injection import (
    PLAN_BLOCK_KWARG,
    PLAN_PREAMBLE,
    inject_plan_block,
    is_plan_block,
    make_plan_block_message,
    plan_block_reserved_tokens,
    render_plan_block,
)
from executor.prompt_cache import _REGISTRY
from executor.prompt_cache.anthropic import AnthropicPromptCacheStrategy
from executor.prompt_cache.bedrock import BedrockPromptCacheStrategy
from executor.prompt_cache.noop import NoopPromptCacheStrategy
from executor.prompt_cache.openai import OpenAIPromptCacheStrategy


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _plan() -> list[dict]:
    return [
        {"id": "a", "title": "Read the config", "status": "completed"},
        {"id": "b", "title": "Set up the schema", "status": "in_progress"},
        {"id": "c", "title": "Write the tests", "status": "pending"},
    ]


def _projection() -> list:
    """A realistic post-hook projection: leading system run + conversation."""
    return [
        SystemMessage(content="platform system prompt"),
        SystemMessage(content="user system prompt"),
        HumanMessage(content="please do the task"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "plan_write", "args": {"items": []}, "id": "tc1"}
            ],
        ),
        ToolMessage(content="Plan updated: 3 item(s) written.", tool_call_id="tc1"),
    ]


def _find_cache_marked_blocks(content):
    """Anthropic shape — blocks whose ``cache_control`` field is set."""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("cache_control")]


def _has_cache_point(content):
    """Bedrock shape — any ``cachePoint`` block present."""
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and "cachePoint" in b for b in content)


# ---------------------------------------------------------------------------
# render_plan_block — canonical Markdown checkbox format (v1 resolution)
# ---------------------------------------------------------------------------


def test_render_completed_is_checked():
    block = render_plan_block(_plan())
    assert "- [x] Read the config" in block


def test_render_pending_is_unchecked():
    block = render_plan_block(_plan())
    assert "- [ ] Write the tests" in block


def test_render_in_progress_unchecked_with_inline_marker():
    block = render_plan_block(_plan())
    assert "- [ ] Set up the schema (in progress)" in block


def test_render_starts_with_preamble():
    block = render_plan_block(_plan())
    assert block.startswith(PLAN_PREAMBLE)


def test_preamble_contains_one_in_progress_guidance():
    """Exactly-one-``in_progress`` is prompt-layer guidance — it lives ONLY
    here (the tool does not enforce it)."""
    assert "exactly one item in_progress" in PLAN_PREAMBLE.lower()


def test_render_no_compaction_language():
    """Neutral framing (silent-compaction rule): the block must never
    mention compaction or summarization."""
    block = render_plan_block(_plan()).lower()
    for banned in ("compact", "summariz", "summaris"):
        assert banned not in block


def test_render_byte_identical_for_equal_plans():
    """Cache stability property 1: unchanged plan → identical bytes."""
    a = render_plan_block(_plan())
    b = render_plan_block(copy.deepcopy(_plan()))
    assert a == b


def test_render_preserves_item_order():
    block = render_plan_block(_plan())
    assert block.index("Read the config") < block.index("Set up the schema")
    assert block.index("Set up the schema") < block.index("Write the tests")


def test_render_unrecognised_status_renders_as_unchecked_pending():
    """Documented defensive behavior: statuses are validated upstream by
    ``plan_write``, but anything unrecognised renders as unchecked-pending
    (no checkbox tick, no ``(in progress)`` marker)."""
    plan = [{"id": "a", "title": "Mystery step", "status": "blocked"}]
    block = render_plan_block(plan)
    assert "- [ ] Mystery step" in block
    assert "(in progress)" not in block
    assert "- [x]" not in block


def test_render_skips_non_dict_items():
    """A corrupted checkpoint must not wedge the task: non-dict items are
    skipped, the rest of the plan still renders."""
    plan = [
        {"id": "a", "title": "Real step", "status": "pending"},
        None,
        "garbage",
        {"id": "b", "title": "Another step", "status": "completed"},
    ]
    block = render_plan_block(plan)
    assert "- [ ] Real step" in block
    assert "- [x] Another step" in block
    assert "garbage" not in block
    # Exactly two checklist lines — the junk items contributed nothing.
    assert sum(line.startswith("- [") for line in block.splitlines()) == 2


def test_render_none_title_does_not_render_the_word_none():
    plan = [{"id": "a", "title": None, "status": "pending"}]
    block = render_plan_block(plan)
    assert "None" not in block
    assert "- [ ]" in block


def test_render_only_checklist_varies_between_plans():
    """The preamble is a stable constant — two different plans share the
    identical preamble prefix; only the checklist body differs."""
    plan_b = _plan()
    plan_b[1]["status"] = "completed"
    a = render_plan_block(_plan())
    b = render_plan_block(plan_b)
    assert a != b
    assert a.startswith(PLAN_PREAMBLE) and b.startswith(PLAN_PREAMBLE)


# ---------------------------------------------------------------------------
# make_plan_block_message / is_plan_block
# ---------------------------------------------------------------------------


def test_make_plan_block_message_is_tagged_human_message():
    msg = make_plan_block_message(_plan())
    # HumanMessage, NOT SystemMessage — see module docstring (langchain
    # anthropic rejects non-leading system messages; memory-flush precedent).
    assert isinstance(msg, HumanMessage)
    assert msg.additional_kwargs.get(PLAN_BLOCK_KWARG) is True
    assert msg.content == render_plan_block(_plan())
    assert is_plan_block(msg)


def test_is_plan_block_false_for_ordinary_messages():
    assert not is_plan_block(HumanMessage(content="hello"))
    assert not is_plan_block(SystemMessage(content="system"))
    assert not is_plan_block(AIMessage(content="hi"))


# ---------------------------------------------------------------------------
# inject_plan_block — the agent_node projection step
# ---------------------------------------------------------------------------


def test_inject_appends_plan_block_at_tail():
    projection = _projection()
    out = inject_plan_block(projection, _plan())
    assert len(out) == len(projection) + 1
    assert out[:-1] == projection
    assert is_plan_block(out[-1])
    assert out[-1].content == render_plan_block(_plan())


def test_inject_empty_plan_is_byte_identical_no_op():
    """Empty plan → NO injection: list byte-identical to the pre-Planning
    shape (no empty SystemMessage, no placeholder)."""
    projection = _projection()
    assert inject_plan_block(projection, []) == projection
    assert inject_plan_block(projection, None) == projection
    assert not any(is_plan_block(m) for m in inject_plan_block(projection, []))


def test_inject_does_not_mutate_inputs():
    """Projection-only: the durable journal (``state['messages']``) is never
    mutated — the helper returns a NEW list and touches no input message."""
    projection = _projection()
    snapshot = list(projection)
    plan = _plan()
    plan_snapshot = copy.deepcopy(plan)

    out = inject_plan_block(projection, plan)

    assert out is not projection
    assert projection == snapshot  # same length, same objects
    assert all(a is b for a, b in zip(projection, snapshot))
    assert plan == plan_snapshot


# ---------------------------------------------------------------------------
# plan_block_reserved_tokens — hard-floor budget accounting (P1 PR-review
# follow-up): the hook must reserve room for the post-hook plan block so a
# projection that passes the hard-floor check can't overflow the provider
# limit once the block is injected.
# ---------------------------------------------------------------------------


def test_reserved_tokens_empty_plan_is_zero_without_estimator_call():
    """Empty/absent plan → 0, and the estimator is provably never invoked —
    non-planning agents pay nothing."""

    def _exploding_estimator(_msgs):
        raise AssertionError("estimator must not be called for empty plans")

    assert plan_block_reserved_tokens([], _exploding_estimator) == 0
    assert plan_block_reserved_tokens(None, _exploding_estimator) == 0


def test_reserved_tokens_estimates_the_exact_injected_block():
    """The reserve is the estimate of the SAME message inject_plan_block
    appends (same renderer, same message shape), under the caller-supplied
    estimator."""
    seen: list = []

    def _estimator(msgs):
        seen.extend(msgs)
        return 123

    assert plan_block_reserved_tokens(_plan(), _estimator) == 123
    assert len(seen) == 1
    assert is_plan_block(seen[0])
    assert seen[0].content == render_plan_block(_plan())


def test_agent_node_reserves_plan_tokens_before_hook_call():
    """agent_node computes the plan reserve BEFORE invoking the hook and
    forwards it as ``reserved_tokens=`` so all trigger / hard-floor
    comparisons account for the block injected afterwards."""
    graph_path = pathlib.Path(__file__).parent.parent / "executor" / "graph.py"
    src = graph_path.read_text()

    reserve_call = src.index("plan_block_reserved_tokens(")
    hook_call = src.index("pass_result = await compaction_pre_model_hook(")
    assert reserve_call < hook_call, (
        "the plan reserve must be computed before the hook call"
    )
    assert "reserved_tokens=" in src[hook_call : hook_call + 1200], (
        "agent_node must forward reserved_tokens= to compaction_pre_model_hook"
    )


# ---------------------------------------------------------------------------
# agent_node seam — injection ordering (source-structure assertion; the
# pattern test_pre_model_hook.py uses because agent_node integration setup
# is large)
# ---------------------------------------------------------------------------


def test_agent_node_injects_after_hook_before_cache_markers():
    """The injection call sits AFTER ``compaction_pre_model_hook`` returns
    (so the block survives Tier 1/3 — the hook already ran) and BEFORE
    ``apply_cache_markers`` (so the block is part of the list the strategy
    marks around)."""
    graph_path = pathlib.Path(__file__).parent.parent / "executor" / "graph.py"
    src = graph_path.read_text()

    hook_call = src.index("pass_result = await compaction_pre_model_hook(")
    inject_call = src.index("inject_plan_block(")
    marker_call = src.index(".apply_cache_markers(")

    assert hook_call < inject_call < marker_call, (
        "agent_node must inject the plan block after compaction_pre_model_hook "
        "returns and before apply_cache_markers runs."
    )


def test_agent_node_passes_injected_list_to_cache_markers():
    """The list handed to ``apply_cache_markers`` (and the no-marker
    fallback when ``WORKER_PROMPT_CACHE_DISABLED=1`` / model unsupported)
    is the plan-injected projection, not the raw hook output."""
    graph_path = pathlib.Path(__file__).parent.parent / "executor" / "graph.py"
    src = graph_path.read_text()

    assert "projected_messages = inject_plan_block(" in src
    marker_window = src[src.index(".apply_cache_markers(") :]
    assert "projected_messages" in marker_window[:200], (
        "apply_cache_markers must receive the plan-injected projection."
    )
    # The no-marker fallback branch must use the same injected projection.
    assert "messages_for_llm = list(projected_messages)" in src, (
        "The markers-disabled fallback must also receive the plan-injected "
        "projection."
    )


# ---------------------------------------------------------------------------
# Cache POSITION — the load-bearing assertions, against the REAL strategies
# ---------------------------------------------------------------------------


def test_anthropic_plan_block_carries_no_breakpoint():
    """With the plan block at the tail, the REAL Anthropic strategy must
    place its stable breakpoint on the last *real* SystemMessage and its
    sliding-window breakpoint on the last *conversation* message — the plan
    block itself stays unmarked, in the uncached suffix."""
    strategy = AnthropicPromptCacheStrategy()
    msgs = inject_plan_block(_projection(), _plan())
    out = strategy.apply_cache_markers(msgs)

    # Plan block (tail): NO marker — content untouched (still a plain str).
    assert is_plan_block(out[-1])
    assert not _find_cache_marked_blocks(out[-1].content)
    assert isinstance(out[-1].content, str)

    # Stable breakpoint: last REAL SystemMessage (idx 1), not the plan block.
    assert _find_cache_marked_blocks(out[1].content)
    assert not _find_cache_marked_blocks(out[0].content)

    # Sliding-window breakpoint: last non-plan message (the ToolMessage).
    assert _find_cache_marked_blocks(out[-2].content)


def test_anthropic_skips_plan_block_even_if_system_typed():
    """Defensive: the skip keys on the tag, not the message class — a
    tagged SystemMessage must not steal the stable breakpoint either."""
    strategy = AnthropicPromptCacheStrategy()
    rogue = SystemMessage(
        content=render_plan_block(_plan()),
        additional_kwargs={PLAN_BLOCK_KWARG: True},
    )
    msgs = [*_projection(), rogue]
    out = strategy.apply_cache_markers(msgs)

    assert not _find_cache_marked_blocks(out[-1].content)
    # Stable breakpoint stays on the real system region.
    assert _find_cache_marked_blocks(out[1].content)
    # Tail breakpoint on the last non-plan message.
    assert _find_cache_marked_blocks(out[-2].content)


def test_anthropic_plan_edit_leaves_marked_prefix_identical():
    """Cache stability property 2 (the changing-plan hazard): editing the
    plan must leave every marked/prefix message byte-identical — only the
    uncached plan suffix differs."""
    strategy = AnthropicPromptCacheStrategy()
    plan_b = _plan()
    plan_b[1]["status"] = "completed"

    out_a = strategy.apply_cache_markers(inject_plan_block(_projection(), _plan()))
    out_b = strategy.apply_cache_markers(inject_plan_block(_projection(), plan_b))

    assert len(out_a) == len(out_b)
    # Everything except the plan block: identical (same content, markers).
    for m_a, m_b in zip(out_a[:-1], out_b[:-1]):
        assert m_a.content == m_b.content
        assert type(m_a) is type(m_b)
    # Only the plan suffix differs.
    assert out_a[-1].content != out_b[-1].content


def test_anthropic_unmarked_behavior_unchanged_without_plan_block():
    """Regression guard: projections with no plan block mark exactly as
    before (last SystemMessage + tail)."""
    strategy = AnthropicPromptCacheStrategy()
    out = strategy.apply_cache_markers(_projection())
    assert _find_cache_marked_blocks(out[1].content)
    assert _find_cache_marked_blocks(out[-1].content)
    assert not _find_cache_marked_blocks(out[0].content)
    assert not _find_cache_marked_blocks(out[2].content)


def test_bedrock_plan_block_carries_no_cache_point():
    """The Bedrock strategy duplicates the two-breakpoint placement with
    ``cachePoint`` blocks — it must skip the plan block the same way."""
    strategy = BedrockPromptCacheStrategy()
    msgs = inject_plan_block(_projection(), _plan())
    out = strategy.apply_cache_markers(msgs)

    assert is_plan_block(out[-1])
    assert not _has_cache_point(out[-1].content)
    assert isinstance(out[-1].content, str)
    # Stable breakpoint on the real system region; tail on the conversation.
    assert _has_cache_point(out[1].content)
    assert _has_cache_point(out[-2].content)
    assert not _has_cache_point(out[0].content)


def test_noop_and_openai_leave_plan_block_unmarked():
    """No-op strategies pass the projection through — the plan block gains
    no marker of any kind."""
    msgs = inject_plan_block(_projection(), _plan())
    for strategy in (NoopPromptCacheStrategy(), OpenAIPromptCacheStrategy()):
        out = strategy.apply_cache_markers(msgs)
        assert out == msgs
        assert isinstance(out[-1].content, str)


@pytest.mark.parametrize(
    "strategy",
    [*_REGISTRY.values(), NoopPromptCacheStrategy()],
    ids=lambda s: s.provider,
)
def test_every_registered_strategy_leaves_plan_block_unchanged(strategy):
    """Structural guard for FUTURE strategies: any strategy registered in
    ``prompt_cache._REGISTRY`` must let the injected plan block survive
    ``apply_cache_markers`` byte-unchanged (no marker, no reshaping) — the
    block lives in the uncached suffix by contract. A new provider that
    forgets to skip ``is_plan_block`` messages fails here instead of
    surfacing as a production cache-cost regression."""
    msgs = inject_plan_block(_projection(), _plan())
    out = strategy.apply_cache_markers(msgs)
    assert is_plan_block(out[-1])
    assert out[-1].content == msgs[-1].content
