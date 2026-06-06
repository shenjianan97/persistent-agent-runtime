"""Planning Primitive — Task P2: post-compaction plan injection.

The agent's ``plan`` channel (Task P1, ``RuntimeState.plan``) is a durable
self-reminder.  This module re-injects it into the LLM-bound projection on
every turn, **after** the Track-7 compaction hook has run (so the block
survives Tier 1/3 — it is rebuilt from the channel, never summarised away)
and **before** prompt-cache markers are applied.

Rendering format — RESOLVED v1 (design open item "Rendering format for
injection"): **Markdown checkbox checklist**.  ``completed`` renders as a
checked box; ``pending`` and ``in_progress`` render unchecked, with
``in_progress`` distinguished by an inline ``(in progress)`` marker.
Rationale: checkbox markdown is the format the model is most fluent in,
compact, and human-legible in replay logs.  :func:`render_plan_block` is the
single canonical format reference — P2's tests and P4's Console parity
reasoning both point here.

Cache position (the load-bearing decision — plan §A4.2 / §A5 row 1)
-------------------------------------------------------------------
The normal case is a *changing* plan (the agent marks items completed and
adds next steps), so the block's bytes change turn-to-turn.  It therefore
must live in the **uncached suffix**: appended at the projection tail, and
skipped by every prompt-cache strategy when breakpoints are chosen (see
``executor/prompt_cache/anthropic.py`` / ``bedrock.py`` — both scans honour
:func:`is_plan_block`).  Inside a marked prefix, every plan edit would
invalidate the conversation cache (full re-prefill); in the uncached suffix
a plan edit re-sends only the block itself — a few hundred tokens, bounded
upstream by P1's 50-item / 200-char caps (no second truncation layer here).

Message type — ``HumanMessage``, deliberately NOT ``SystemMessage``
-------------------------------------------------------------------
``langchain_anthropic`` (1.3.4, ``chat_models.py::_format_messages``) raises
``ValueError("Received multiple non-consecutive system messages.")`` for any
system message appearing after a non-system turn — and the uncached suffix
is by definition after the conversation.  This mirrors the Track-7
pre-Tier-3 memory-flush precedent (``executor/compaction/pre_model_hook.py``,
"Must NOT be a SystemMessage"): emit a ``HumanMessage`` that Anthropic's
``_merge_messages`` folds into the preceding user/tool turn, while OpenAI
accepts consecutive user turns natively.  The :data:`PLAN_BLOCK_KWARG`
marker on ``additional_kwargs`` lets the cache strategies (and any future
consumer) identify the block structurally.

Framing is neutral (the silent-compaction rule): a plain "current plan"
reminder — never "you are being compacted".  The exactly-one-``in_progress``
rule is prompt-layer guidance expressed ONLY in :data:`PLAN_PREAMBLE`
(``plan_write`` deliberately does not enforce it — see Task P1).  The
preamble is a stable constant so the only thing that ever changes in the
block is the checklist body (cache-friendly byte-stability for unchanged
plans).
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage

#: ``additional_kwargs`` tag identifying the injected plan block.  The
#: prompt-cache strategies skip tagged messages when placing breakpoints so
#: the block stays in the uncached suffix.  Never serialized to providers —
#: ``additional_kwargs`` on human messages is dropped at request-format time
#: (same mechanism the memory-flush ``compaction_event`` tag relies on).
PLAN_BLOCK_KWARG: str = "plan_block"

#: Stable preamble line.  Deterministic — no per-turn variation — so an
#: unchanged plan renders byte-identical bytes across turns.  This is the
#: ONLY place the exactly-one-``in_progress`` guidance is expressed.
PLAN_PREAMBLE: str = (
    "Current plan — your own to-do list, maintained with the plan_write "
    "tool. Keep exactly one item in_progress. As you make progress, rewrite "
    "the plan with plan_write to keep it current."
)


def render_plan_block(plan_items: Sequence[dict]) -> str:
    """Render *plan_items* as the canonical plan block (preamble + checklist).

    Pure and deterministic: output depends only on the item list (order
    preserved, no timestamps, no nondeterministic iteration), so equal plans
    produce byte-identical strings — cache stability property 1.

    Per-item rendering (the v1 format resolution):

    * ``completed``   → ``- [x] {title}``
    * ``in_progress`` → ``- [ ] {title} (in progress)``
    * ``pending``     → ``- [ ] {title}``

    Statuses are validated upstream by ``plan_write`` (Task P1); anything
    unrecognised renders defensively as unchecked-pending.
    """
    lines = [PLAN_PREAMBLE, ""]
    for item in plan_items:
        title = item.get("title", "")
        status = item.get("status")
        if status == "completed":
            lines.append(f"- [x] {title}")
        elif status == "in_progress":
            lines.append(f"- [ ] {title} (in progress)")
        else:
            lines.append(f"- [ ] {title}")
    return "\n".join(lines)


def make_plan_block_message(plan_items: Sequence[dict]) -> HumanMessage:
    """Build the tagged plan-block message for projection injection."""
    return HumanMessage(
        content=render_plan_block(plan_items),
        additional_kwargs={PLAN_BLOCK_KWARG: True},
    )


def is_plan_block(message: BaseMessage) -> bool:
    """True when *message* is an injected plan block (tag-keyed, not
    type-keyed, so the check is robust to message-class changes)."""
    kwargs = getattr(message, "additional_kwargs", None) or {}
    return bool(kwargs.get(PLAN_BLOCK_KWARG))


def inject_plan_block(
    messages: Sequence[BaseMessage],
    plan_items: Sequence[dict] | None,
) -> list[BaseMessage]:
    """Return the projection with the plan block appended at the tail.

    * Non-empty plan → new list ``[*messages, plan_block]``.
    * Empty / absent plan → a plain copy, byte-identical to the input shape
      (no placeholder, no empty message — agents that never call
      ``plan_write`` keep their exact pre-Planning prompt).

    Pure: never mutates *messages* (the durable journal is owned by
    LangGraph; injection is a per-call projection addendum only).
    """
    if not plan_items:
        return list(messages)
    return [*messages, make_plan_block_message(plan_items)]


__all__ = [
    "PLAN_BLOCK_KWARG",
    "PLAN_PREAMBLE",
    "inject_plan_block",
    "is_plan_block",
    "make_plan_block_message",
    "render_plan_block",
]
