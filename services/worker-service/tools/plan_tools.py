"""Planning Primitive — Task P1: ``plan_write`` tool.

The Planning Primitive gives a ReAct agent a durable scratchpad: a flat list
of plan items the agent rewrites whenever it calls ``plan_write``.  The plan
is **not load-bearing** — nothing enforces it; it is a self-reminder injected
back into the prompt after compaction (task P2) so the agent does not lose its
own to-do list across a context-window transform.

**Write semantics — full-list replace (Claude Code ``TodoWrite`` shape):**
Each ``plan_write`` call carries the *entire* plan and overwrites the
``RuntimeState.plan`` channel verbatim.  This resolves the design's open
question "Write semantics: full-list replace vs. patch ops"
(``docs/design-docs/agent-modes/design.md`` → *What stays open for the
Planning Primitive's own design pass*).

Rationale: a flat self-reminder has no merge concerns; replace keeps the
injected block byte-stable between unchanged writes (cache-friendly, mirrors
``_list_replace_reducer``'s rationale); it sidesteps patch-conflict semantics
the plan does not need.

**Size caps — v1:** max **50 items**, title max **200 characters** per item.
Exceeding either cap is a tool-layer rejection (returns a structured error the
LLM can correct), not silent truncation.  This resolves the design's open
question "Plan size limits".

**One-in-progress NOT enforced:** the tool accepts zero, one, or many
``in_progress`` items without complaint.  Exactly-one-``in_progress`` is
prompt-layer guidance delivered in P2's injected preamble.
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — v1 caps (resolved open items from the design doc)
# ---------------------------------------------------------------------------

#: Maximum number of items in a single ``plan_write`` call.
#: Resolves design open item "Plan size limits: item count".
PLAN_MAX_ITEMS: int = 50

#: Maximum number of characters per item title.
#: Resolves design open item "Plan size limits: content length".
PLAN_MAX_TITLE_CHARS: int = 200

#: Valid values for item ``status``.  Any other value is a tool-layer rejection.
VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanWriteError(ValueError):
    """Raised for structural plan-write validation failures.

    These are *structural* rejections (the tool returns an error result the
    LLM can correct), not silent truncation.
    """


# ---------------------------------------------------------------------------
# Item shape
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    """A single plan item.

    ``id`` is agent-supplied so a rewrite can preserve item identity across
    calls (the design favors agent-supplied ids so P3's API and P4's Console
    ``data-testid="plan-item-{id}"`` key on them).
    """

    id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable identifier supplied by the agent. Preserved across rewrites "
            "so the read API and Console can track item identity."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=PLAN_MAX_TITLE_CHARS,
        description=(
            f"The item text. Max {PLAN_MAX_TITLE_CHARS} characters."
        ),
    )
    status: str = Field(
        ...,
        description=(
            "Item status. Must be one of: pending, in_progress, completed."
        ),
    )


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------


PLAN_WRITE_DESCRIPTION: str = (
    "Rewrite your entire plan. Each call replaces the plan channel wholesale "
    "(full-list replace — always supply ALL items, not a delta). Items are "
    "{id, title, status}; status must be one of: pending, in_progress, "
    "completed. The plan survives context compaction and is injected back "
    "into your prompt so you never lose your to-do list. "
    f"Caps: max {PLAN_MAX_ITEMS} items; titles max {PLAN_MAX_TITLE_CHARS} chars. "
    "Call freely — zero cost, no I/O."
)


class PlanWriteArguments(BaseModel):
    """Input schema for ``plan_write``.

    ``items`` is the *entire* plan — full-list replace semantics.
    """

    items: list[dict] = Field(
        default_factory=list,
        description=(
            "The complete plan as a list of {id, title, status} items. "
            "Replaces the current plan entirely."
        ),
    )
    # Injected by ToolNode at runtime; hidden from the LLM schema so the model
    # never tries to supply it. Required so the tool can return a matching
    # ``ToolMessage`` paired to the agent's tool call.
    tool_call_id: Annotated[str, InjectedToolCallId]


# ---------------------------------------------------------------------------
# Validation helper (importable by tests and by the tool handler)
# ---------------------------------------------------------------------------


def validate_plan_items(items: list[dict]) -> None:
    """Validate a list of plan items.

    Raises :class:`PlanWriteError` on the first structural violation.
    Validation rules:

    * Item count ≤ ``PLAN_MAX_ITEMS`` (50).
    * Each item has a ``title`` and ``status``.
    * ``status`` must be in ``VALID_STATUSES`` (``pending`` | ``in_progress`` |
      ``completed``).
    * ``title`` length ≤ ``PLAN_MAX_TITLE_CHARS`` (200 characters).

    **Does NOT enforce exactly-one-``in_progress``.**  That rule is
    prompt-layer guidance delivered in P2's injected preamble.

    :param items: List of plan item dicts, each expected to have ``id``,
        ``title``, and ``status`` keys.
    :raises PlanWriteError: If any validation rule is violated.
    """
    if len(items) > PLAN_MAX_ITEMS:
        raise PlanWriteError(
            f"plan_write rejected: {len(items)} items exceeds the "
            f"{PLAN_MAX_ITEMS}-item cap. Submit a plan with at most "
            f"{PLAN_MAX_ITEMS} items."
        )

    for i, item in enumerate(items):
        # Validate status
        status = item.get("status")
        if status not in VALID_STATUSES:
            allowed = ", ".join(sorted(VALID_STATUSES))
            raise PlanWriteError(
                f"plan_write rejected: item {i} has invalid status "
                f"{status!r}. Allowed values: {allowed}."
            )

        # Validate title length
        title = item.get("title", "")
        if len(title) > PLAN_MAX_TITLE_CHARS:
            raise PlanWriteError(
                f"plan_write rejected: item {i} title is {len(title)} "
                f"characters, which exceeds the {PLAN_MAX_TITLE_CHARS}-character cap."
            )

        # Validate title is present (missing title key or empty string)
        if not title:
            raise PlanWriteError(
                f"plan_write rejected: item {i} has a missing or empty title."
            )

        # Validate id is present
        id_ = item.get("id")
        if not id_:
            raise PlanWriteError(
                f"plan_write rejected: item {i} has a missing or empty id."
            )


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


def _plan_write_handler(
    items: list[dict],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Handle a ``plan_write`` tool call.

    Validates the incoming item list, writes it verbatim into
    ``state["plan"]`` via a LangGraph ``Command`` update, and returns a
    short confirmation ``ToolMessage`` so the LLM knows the write landed.

    **Does NOT transform, re-order, or normalize item content** beyond
    validation.  The plan channel is written verbatim.

    LangGraph's ``ToolNode`` requires a matching ``ToolMessage`` in the
    ``Command``'s ``messages`` update — without it the next agent step
    rejects the orphan tool call as a fatal graph error.
    """
    # Validate — raises PlanWriteError on violation (ToolNode surfaces it back
    # to the agent as a tool-result error so the graph stays in-loop).
    validate_plan_items(items)

    item_count = len(items)
    logger.debug(
        "plan.write.applied item_count=%d",
        item_count,
    )

    confirmation = (
        f"Plan updated: {item_count} item(s) written."
    )

    return Command(update={
        "messages": [
            ToolMessage(
                content=confirmation,
                tool_call_id=tool_call_id,
            )
        ],
        # Write the plan verbatim — full-list replace.
        "plan": items,
    })


# ---------------------------------------------------------------------------
# StructuredTool factory
# ---------------------------------------------------------------------------


def build_plan_write_tool() -> StructuredTool:
    """Build the ``plan_write`` ``StructuredTool`` for registration in ``_get_tools``.

    The tool is stateless — no closure bindings are required.  It writes
    directly into ``RuntimeState.plan`` via a ``Command`` update.

    Registration is allowlist-gated in ``executor.graph._get_tools``:
    agents without ``"plan_write"`` in their ``allowed_tools`` never see
    this tool.
    """
    return StructuredTool.from_function(
        func=_plan_write_handler,
        name="plan_write",
        description=PLAN_WRITE_DESCRIPTION,
        args_schema=PlanWriteArguments,
    )


__all__ = [
    "PLAN_MAX_ITEMS",
    "PLAN_MAX_TITLE_CHARS",
    "PLAN_WRITE_DESCRIPTION",
    "VALID_STATUSES",
    "PlanItem",
    "PlanWriteArguments",
    "PlanWriteError",
    "build_plan_write_tool",
    "validate_plan_items",
]
