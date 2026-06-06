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

**Ids must be unique within a plan** — they are the stable-identity key that
P3's read API and P4's Console ``data-testid="plan-item-{id}"`` key on;
duplicates are a tool-layer rejection.

**One-in-progress NOT enforced:** the tool accepts zero, one, or many
``in_progress`` items without complaint.  Exactly-one-``in_progress`` is
prompt-layer guidance delivered in P2's injected preamble.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal, get_args

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

#: Canonical status enum. ``PlanItem.status`` is typed with this Literal and
#: ``VALID_STATUSES`` is DERIVED from it — single source of truth, so the two
#: validation layers (pydantic args schema + ``validate_plan_items``) cannot
#: drift.
PlanStatus = Literal["pending", "in_progress", "completed"]

#: Valid values for item ``status``.  Any other value is a tool-layer rejection.
#: Derived from :data:`PlanStatus` so the LLM-visible enum and the dict-level
#: validator always agree.
VALID_STATUSES: frozenset[str] = frozenset(get_args(PlanStatus))


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
    """A single plan item — the element type of ``PlanWriteArguments.items``.

    ``id`` is agent-supplied so a rewrite can preserve item identity across
    calls (the design favors agent-supplied ids so P3's API and P4's Console
    ``data-testid="plan-item-{id}"`` key on them).  **Ids must be unique
    within a plan** — downstream surfaces key on them; duplicates are rejected
    by :func:`validate_plan_items`.

    Validation layering (documented decision): this model's constraints
    *derive from the same constants* as :func:`validate_plan_items`
    (``PLAN_MAX_TITLE_CHARS`` referenced directly; ``status`` typed as
    :data:`PlanStatus`, from which ``VALID_STATUSES`` is derived) — the two
    layers cannot drift.  On the LLM path, pydantic parsing is the first gate
    (structured schema, typed errors); ``validate_plan_items`` is the
    authoritative dict-level enforcer the handler always runs, and the only
    place for cross-item rules (item-count cap, duplicate ids).
    """

    id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable identifier supplied by the agent, unique within the plan. "
            "Preserved across rewrites so the read API and Console can track "
            "item identity."
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
    status: PlanStatus = Field(
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

    ``items`` is the *entire* plan — full-list replace semantics.  It is
    **required** (no default): a malformed call that omits ``items`` is a
    schema error the LLM can correct, NOT a silent plan-clear.  Clearing the
    plan requires an explicit ``items=[]``.
    """

    items: list[PlanItem] = Field(
        ...,
        description=(
            "The complete plan as a list of {id, title, status} items. "
            "Replaces the current plan entirely. Pass [] explicitly to "
            "clear the plan."
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
    """Validate a list of plan items (dict-level — the authoritative enforcer).

    Raises :class:`PlanWriteError` on the first structural violation.
    Validation rules:

    * Item count ≤ ``PLAN_MAX_ITEMS`` (50).
    * Each item has a non-empty string ``id``, ``title``, and ``status``
      (non-string values are rejected with an error naming the field and the
      expected type — never a bare ``TypeError``).
    * ``status`` must be in ``VALID_STATUSES`` (``pending`` | ``in_progress`` |
      ``completed``).
    * ``title`` length ≤ ``PLAN_MAX_TITLE_CHARS`` (200 characters).
    * **Ids must be unique within the plan.** They are the stable-identity
      key the read API (P3) and the Console's ``data-testid="plan-item-{id}"``
      (P4) key on — duplicates would break that contract, so they are
      rejected.

    **Does NOT enforce exactly-one-``in_progress``.**  That rule is
    prompt-layer guidance delivered in P2's injected preamble.

    Layering note: on the LLM invoke path the pydantic args schema
    (:class:`PlanItem`) has already enforced the per-item rules — both layers
    derive from the same constants so they cannot drift.  This function is
    kept as the dict-level single source of truth for direct callers (tests,
    future projections) and is the only place for cross-item rules (count
    cap, duplicate ids).

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

    seen_ids: set[str] = set()
    for i, item in enumerate(items):
        # Validate id — must be a non-empty string (type-checked first so a
        # non-string never reaches truthiness/len with a confusing TypeError).
        id_ = item.get("id")
        if not isinstance(id_, str) or not id_:
            raise PlanWriteError(
                f"plan_write rejected: item {i} field 'id' must be a "
                f"non-empty string, got {id_!r}."
            )

        # Duplicate-id check — ids are the stable-identity key downstream
        # surfaces (P3 API, P4 Console data-testid) key on.
        if id_ in seen_ids:
            raise PlanWriteError(
                f"plan_write rejected: duplicate item id {id_!r}. Ids must "
                f"be unique within the plan — they identify items across "
                f"rewrites."
            )
        seen_ids.add(id_)

        # Validate title — must be a non-empty string within the cap.
        title = item.get("title")
        if not isinstance(title, str) or not title:
            raise PlanWriteError(
                f"plan_write rejected: item {i} field 'title' must be a "
                f"non-empty string, got {title!r}."
            )
        if len(title) > PLAN_MAX_TITLE_CHARS:
            raise PlanWriteError(
                f"plan_write rejected: item {i} title is {len(title)} "
                f"characters, which exceeds the {PLAN_MAX_TITLE_CHARS}-character cap."
            )

        # Validate status — must be a string in the enum.
        status = item.get("status")
        if not isinstance(status, str):
            raise PlanWriteError(
                f"plan_write rejected: item {i} field 'status' must be a "
                f"string, got {status!r}."
            )
        if status not in VALID_STATUSES:
            allowed = ", ".join(sorted(VALID_STATUSES))
            raise PlanWriteError(
                f"plan_write rejected: item {i} has invalid status "
                f"{status!r}. Allowed values: {allowed}."
            )


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


def _plan_write_handler(
    items: list,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Handle a ``plan_write`` tool call.

    Validates the incoming item list, writes it verbatim into
    ``state["plan"]`` via a LangGraph ``Command`` update, and returns a
    short confirmation ``ToolMessage`` so the LLM knows the write landed.

    On the LLM invoke path the args schema has parsed each item into a
    :class:`PlanItem`; those are dumped back to plain dicts before the write
    so the checkpoint JSONB stays codec-free — the dict content is identical
    to what the LLM sent (verbatim contract preserved).  Direct callers may
    pass plain dicts, which flow through unchanged.

    **Does NOT transform, re-order, or normalize item content** beyond
    validation.  The plan channel is written verbatim.

    LangGraph's ``ToolNode`` requires a matching ``ToolMessage`` in the
    ``Command``'s ``messages`` update — without it the next agent step
    rejects the orphan tool call as a fatal graph error.
    """
    # Normalize PlanItem models (LLM invoke path) to plain dicts; plain dicts
    # (direct callers) pass through unchanged.
    plain_items: list[dict] = [
        item.model_dump() if isinstance(item, PlanItem) else item
        for item in items
    ]

    # Validate — raises PlanWriteError on violation (ToolNode surfaces it back
    # to the agent as a tool-result error so the graph stays in-loop).
    validate_plan_items(plain_items)

    item_count = len(plain_items)
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
        "plan": plain_items,
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
    "PlanStatus",
    "PlanWriteArguments",
    "PlanWriteError",
    "build_plan_write_tool",
    "validate_plan_items",
]
