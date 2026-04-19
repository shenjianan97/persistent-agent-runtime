"""Phase 2 Track 5 Task 7 — worker-side memory tools.

Three built-in tools registered per-task with ``(tenant_id, agent_id)`` scope
bound from the worker's **immutable** task context at registration time:

1. ``memory_note(text)`` — state-mutating. Returns
   ``Command(update={"observations": [text]})`` so LangGraph's ``operator.add``
   reducer on :class:`executor.memory_graph.MemoryEnabledState` merges the
   append. Zero cost (no LLM, no network).
2. ``memory_search(query, mode='hybrid', limit=5)`` — delegates to the Memory
   REST API (Task 3) using the worker's HTTP client. The ``agent_id`` in the
   URL path is the bound value — the LLM cannot override it. A hybrid request
   that hits an embedding outage silently degrades server-side to ``mode=text``
   (the API returns ``ranking_used="text"``); an explicit ``mode=vector``
   under the same failure surfaces as a tool error shaped as a recoverable
   hint to retry with ``mode=text``.
3. ``task_history_get(task_id)`` — bounded diagnostic. Always registered
   (even for memory-disabled agents). Queries ``tasks`` via asyncpg with a
   hard ``WHERE ... AND tenant_id = :bound AND agent_id = :bound`` predicate;
   cross-scope lookups return a uniform tool-shaped "not found" error — the
   404-not-403 rule at the tool surface.

Scope-binding invariant
-----------------------
``tenant_id`` and ``agent_id`` come from the worker's task context at the
moment :func:`build_memory_tools` is called. They are captured by closure in
each tool function. LLM-supplied arguments (``query``, ``task_id``, ``mode``,
``limit``, ``text``) cannot broaden scope — ``memory_search`` always hits
the bound ``agent_id`` URL, and ``task_history_get`` always appends the
bound predicate to its SQL. See ``docs/design-docs/phase-2/track-5-memory.md``
§ "Agent Tools" for the full contract.

Registration gating
-------------------
``memory_note`` and ``memory_search`` are returned whenever the stack is
enabled (``decision.stack_enabled=True``) — i.e., ``agent.memory.enabled`` AND
``task.memory_mode ∈ {always, agent_decides}``. ``save_memory`` (Task 12) is
returned only when the stack is enabled AND ``auto_write=False``
(``agent_decides`` mode), so the agent has a lever to opt in per run.
``task_history_get`` is always returned — it is a diagnostic drill-down, not
a memory tool, and the scope binding keeps it safe regardless of memory
state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable

import asyncpg
import httpx
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from tools.task_history_reader import read_tool_calls as _read_tool_calls

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool argument shapes (Pydantic — enforced by LangChain's StructuredTool)
# ---------------------------------------------------------------------------

# Hard caps match the design doc § "Agent Tools" and § "Validation".
MEMORY_NOTE_MAX_LEN = 2048
MEMORY_SEARCH_QUERY_MAX_LEN = 400
MEMORY_SEARCH_TOOL_LIMIT_MAX = 10  # REST API allows 20; tool keeps surface small.
MEMORY_SEARCH_DEFAULT_LIMIT = 5
TASK_HISTORY_INPUT_TRUNCATE_BYTES = 2048
TASK_HISTORY_OUTPUT_TRUNCATE_BYTES = 2048
TASK_HISTORY_TOOL_CALLS_CAP = 20
TASK_HISTORY_PREVIEW_BYTES = 512

_ALLOWED_MODES = ("hybrid", "text", "vector")


class MemoryNoteArguments(BaseModel):
    text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MEMORY_NOTE_MAX_LEN,
            description=(
                "A short, durable note to append to this task's draft memory "
                "entry. Persisted across worker restarts. "
                f"Max {MEMORY_NOTE_MAX_LEN} characters."
            ),
        ),
    ]
    # Injected by ToolNode at runtime; hidden from the LLM schema so the model
    # never tries to supply it. Required so the tool can return a matching
    # ``ToolMessage`` paired to the agent's tool call — LangGraph's ``ToolNode``
    # rejects a ``Command`` update that lacks the pairing.
    tool_call_id: Annotated[str, InjectedToolCallId]


# Phase 2 Track 5 Task 12 — ``save_memory`` mirror shape. Same 1..2048 char
# bound as ``memory_note`` so a reason fits in the observations channel too.
SAVE_MEMORY_REASON_MAX_LEN = 2048


class SaveMemoryArguments(BaseModel):
    reason: Annotated[
        str,
        Field(
            min_length=1,
            max_length=SAVE_MEMORY_REASON_MAX_LEN,
            description=(
                "Short justification for why this run is worth remembering. "
                "Flows into the task timeline and the summarizer input as "
                f"an observation. Max {SAVE_MEMORY_REASON_MAX_LEN} characters."
            ),
        ),
    ]
    # Injected by ToolNode at runtime; hidden from the LLM schema. See
    # ``MemoryNoteArguments.tool_call_id`` for rationale.
    tool_call_id: Annotated[str, InjectedToolCallId]


class MemorySearchArguments(BaseModel):
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MEMORY_SEARCH_QUERY_MAX_LEN,
            description="Search query over this agent's past memory entries.",
        ),
    ]
    mode: Annotated[
        str,
        Field(
            default="hybrid",
            description=(
                "Ranking mode: 'hybrid' (default, BM25 + vector via RRF), "
                "'text' (BM25 only), or 'vector' (embedding only). "
                "If 'vector' returns an error because the embedding provider "
                "is down, retry with 'text'."
            ),
        ),
    ] = "hybrid"
    limit: Annotated[
        int,
        Field(
            default=MEMORY_SEARCH_DEFAULT_LIMIT,
            ge=1,
            le=MEMORY_SEARCH_TOOL_LIMIT_MAX,
            description=(
                f"Maximum number of results to return "
                f"(max {MEMORY_SEARCH_TOOL_LIMIT_MAX})."
            ),
        ),
    ] = MEMORY_SEARCH_DEFAULT_LIMIT


class TaskHistoryGetArguments(BaseModel):
    task_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            description=(
                "The UUID of a past task scoped to this agent. "
                "Cross-agent or cross-tenant ids return 'not found'."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Tool-context container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryToolContext:
    """Per-task context captured at tool-registration time.

    Every field here is immutable for the life of the task — LLM arguments
    cannot override them. ``build_memory_tools`` closes over an instance and
    embeds the bound values into each tool's URL / SQL.
    """

    tenant_id: str
    agent_id: str
    task_id: str
    pool: asyncpg.Pool
    memory_api_base_url: str
    http_client: httpx.AsyncClient
    cancel_event: asyncio.Event | None = None
    # Optional — if provided, each HTTP / DB awaitable is run through this
    # helper so a cancellation event tears the tool call down cleanly.
    # Matches the pattern already used for MCP tool invocations.
    await_or_cancel_fn: Callable[..., Awaitable[Any]] | None = None
    # Optional — the LangGraph checkpointer. When present, ``task_history_get``
    # uses it to populate ``tool_calls`` from the target task's message
    # history. Safe to omit: the tool falls back to an empty list with a
    # structured log so a LangGraph-version drift can only degrade, never
    # crash.
    checkpointer: Any = None


# ---------------------------------------------------------------------------
# Errors surfaced as tool errors (LangGraph's ToolNode passes them back to
# the agent so the graph stays in-loop — see ``_handle_tool_error`` in
# :mod:`executor.graph`).
# ---------------------------------------------------------------------------


class MemoryToolError(RuntimeError):
    """Base class for tool-shaped, recoverable memory errors."""


class MemoryToolNotFoundError(MemoryToolError):
    """Uniform 'not found' — used for every scope-miss across tools."""


class MemorySearchVectorUnavailableError(MemoryToolError):
    """Raised when vector-mode search requested but embedding provider is down."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if len(text.encode("utf-8")) <= limit:
        return text
    # Byte-safe truncate: slice on characters, then shrink until under limit.
    truncated = text[:limit]
    while len(truncated.encode("utf-8")) > limit:
        truncated = truncated[:-1]
    if len(truncated) < len(text):
        return truncated + "...[truncated]"
    return truncated


def _preview_json(value: Any, limit: int) -> str:
    try:
        if isinstance(value, str):
            serialized = value
        else:
            serialized = json.dumps(value, default=str)
    except Exception:
        serialized = str(value)
    return _truncate(serialized, limit) or ""


def _maybe_await_or_cancel(
    ctx: MemoryToolContext,
    awaitable: Awaitable[Any],
    *,
    operation: str,
) -> Awaitable[Any]:
    """Route an awaitable through the worker's cancellation helper if wired."""
    if ctx.await_or_cancel_fn is not None and ctx.cancel_event is not None:
        return ctx.await_or_cancel_fn(
            awaitable,
            ctx.cancel_event,
            task_id=ctx.task_id,
            operation=operation,
        )
    return awaitable


# ---------------------------------------------------------------------------
# memory_note
# ---------------------------------------------------------------------------


MEMORY_NOTE_DESCRIPTION = (
    "Append a short observation to this task's draft memory entry. "
    "Use this to capture salient intermediate findings that should survive "
    "into the final retrospective memory. Returns {ok, count}. "
    f"Max {MEMORY_NOTE_MAX_LEN} characters per note. Zero cost."
)


def _build_memory_note_tool(ctx: MemoryToolContext) -> StructuredTool:
    # ``tenant_id`` / ``agent_id`` are captured for logging. The tool does
    # NOT touch the DB — the ``operator.add`` reducer on the
    # ``observations`` state field merges the append at super-step commit.
    bound_tenant = ctx.tenant_id
    bound_agent = ctx.agent_id
    bound_task = ctx.task_id

    def memory_note(
        text: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        # Argument validation is enforced by ``MemoryNoteArguments`` — if we
        # reach here, ``text`` already satisfies length 1..MEMORY_NOTE_MAX_LEN.
        # LangGraph's ``ToolNode`` requires a matching ``ToolMessage`` in the
        # Command's ``messages`` update — without it the next agent step
        # rejects the orphan tool call as a fatal graph error.
        logger.debug(
            "memory.note.appended tenant_id=%s agent_id=%s task_id=%s "
            "note_chars=%d",
            bound_tenant, bound_agent, bound_task, len(text),
        )
        return Command(update={
            "messages": [ToolMessage(content="ok", tool_call_id=tool_call_id)],
            "observations": [text],
        })

    return StructuredTool.from_function(
        func=memory_note,
        name="memory_note",
        description=MEMORY_NOTE_DESCRIPTION,
        args_schema=MemoryNoteArguments,
    )


# ---------------------------------------------------------------------------
# save_memory (Phase 2 Track 5 Task 12)
# ---------------------------------------------------------------------------


SAVE_MEMORY_DESCRIPTION = (
    "Opt this task in to writing a durable retrospective memory entry. "
    "Call this exactly when the run has produced something worth "
    "remembering (non-trivial findings, customer decisions, recurring "
    "patterns). Silently no-ops when called more than once. Argument: "
    "reason (1-2048 chars) — a short justification that will appear in "
    "the task timeline and feed the summarizer. Zero cost; the write "
    "itself happens once at the terminal branch."
)


def _build_save_memory_tool(ctx: MemoryToolContext) -> StructuredTool:
    """Task 12 — agent-decides opt-in tool.

    Returns a :class:`Command` that sets ``memory_opt_in=True`` on the state
    (simple last-write-wins overwrite) AND appends the reason to
    ``observations`` so the opt-in is visible in the task timeline as a
    ``ToolMessage`` and feeds the summarizer alongside the rest of the
    observations. The reason is ``strip()``-ed to normalize agent whitespace;
    length bounds (1..2048 chars after stripping) are enforced by
    :class:`SaveMemoryArguments` — the Pydantic schema reports an error
    through LangGraph's ToolNode, keeping the graph in-loop.

    The reason is NOT persisted into ``agent_memory_entries``. It lives only
    in the observations snapshot — the summarizer may reference it freely
    when composing the final memory body.
    """

    bound_tenant = ctx.tenant_id
    bound_agent = ctx.agent_id
    bound_task = ctx.task_id

    def save_memory(
        reason: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        stripped = reason.strip()
        if not stripped:
            # Schema already rejects empty strings; post-strip emptiness
            # (e.g. reason="   ") lands here. Surface as a tool error so the
            # agent can self-correct in-loop rather than silently opting in
            # with a blank rationale.
            raise MemoryToolError(
                "save_memory requires a non-empty reason after whitespace is "
                "stripped."
            )
        if len(stripped) > SAVE_MEMORY_REASON_MAX_LEN:
            # Defense in depth — normally caught by the Pydantic max_length
            # check, but stripping on our side can never lengthen so this
            # branch is unreachable in practice. Keep the guard so the
            # invariant is obvious.
            raise MemoryToolError(
                f"save_memory reason exceeds {SAVE_MEMORY_REASON_MAX_LEN} chars."
            )
        logger.info(
            "memory.save_memory.opt_in tenant_id=%s agent_id=%s task_id=%s "
            "reason_chars=%d",
            bound_tenant, bound_agent, bound_task, len(stripped),
        )
        # LangGraph's ``ToolNode`` requires a matching ``ToolMessage`` in the
        # Command's ``messages`` update — every LLM tool call needs a paired
        # reply or the next agent step rejects the orphan as a fatal graph
        # error (surfaced on the dead_letter task
        # c385a4e4-e88c-4a2a-9796-f409ebec18cc).
        return Command(
            update={
                "messages": [
                    ToolMessage(content="ok", tool_call_id=tool_call_id),
                ],
                "memory_opt_in": True,
                "observations": [f"[save_memory] {stripped}"],
            }
        )

    return StructuredTool.from_function(
        func=save_memory,
        name="save_memory",
        description=SAVE_MEMORY_DESCRIPTION,
        args_schema=SaveMemoryArguments,
    )


# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------


MEMORY_SEARCH_DESCRIPTION = (
    "Search this agent's past memory entries via hybrid (BM25 + vector), "
    "text-only, or vector-only ranking. Returns a list of hits with "
    "{memory_id, title, summary_preview, outcome, task_id, created_at, "
    "score}. If an explicit 'vector' request fails because the embedding "
    "provider is down, retry with 'text'. "
    f"Limit capped at {MEMORY_SEARCH_TOOL_LIMIT_MAX}."
)


def _build_memory_search_tool(ctx: MemoryToolContext) -> StructuredTool:
    bound_tenant = ctx.tenant_id
    bound_agent = ctx.agent_id
    bound_task = ctx.task_id

    # Normalize base URL — trailing slash stripped so we can safely join paths.
    base_url = ctx.memory_api_base_url.rstrip("/")

    async def memory_search(
        query: str,
        mode: str = "hybrid",
        limit: int = MEMORY_SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        # Defense-in-depth — the Pydantic schema already validates ``mode``
        # is one of the three allowed modes, but the LLM can sometimes skip
        # schema constraints when the SDK doesn't enforce enums. Fail loud
        # here with a usable message.
        if mode not in _ALLOWED_MODES:
            raise MemoryToolError(
                f"Unsupported mode {mode!r}. Use one of: {', '.join(_ALLOWED_MODES)}."
            )
        effective_limit = max(1, min(int(limit), MEMORY_SEARCH_TOOL_LIMIT_MAX))

        # URL uses the BOUND agent_id. The LLM cannot override this — it is
        # closed over from the worker task context. Tenant scope is enforced
        # server-side by the API's existing auth/scope path.
        url = f"{base_url}/v1/agents/{bound_agent}/memory/search"
        params = {"q": query, "mode": mode, "limit": str(effective_limit)}

        try:
            response = await _maybe_await_or_cancel(
                ctx,
                ctx.http_client.get(url, params=params),
                operation="memory_search",
            )
        except httpx.RequestError as exc:
            logger.warning(
                "memory.search.transport_error tenant_id=%s agent_id=%s "
                "task_id=%s error_class=%s",
                bound_tenant, bound_agent, bound_task, type(exc).__name__,
            )
            raise MemoryToolError(
                f"Memory search transport error: {type(exc).__name__}. "
                "Retry momentarily."
            ) from exc

        # Translate HTTP surface into tool-shaped outcomes.
        if response.status_code == 503:
            # Embedding provider down + mode=vector — recoverable by retry
            # with mode=text per the design doc contract.
            logger.info(
                "memory.search.vector_unavailable tenant_id=%s agent_id=%s "
                "task_id=%s mode=%s",
                bound_tenant, bound_agent, bound_task, mode,
            )
            raise MemorySearchVectorUnavailableError(
                "Embedding provider unavailable for vector-mode search. "
                "Retry with mode='text' to search BM25-only, or 'hybrid' "
                "to let the server silently degrade when embeddings return."
            )
        if response.status_code == 404:
            # 404 here means the API could not resolve the agent in the
            # caller's tenant scope. Since ``bound_agent`` is the worker's
            # OWN agent (the task is running under it), this should never
            # happen in steady state — it usually points at a configuration
            # drift (tenant-id header vs DEFAULT_TENANT_ID, agent soft-
            # deleted mid-task, etc.). Log at WARNING so it's greppable in
            # ops, but still return the uniform empty-result shape so the
            # agent loop stays in-flight and doesn't leak the specific cause.
            logger.warning(
                "memory.search.scope_unresolved tenant_id=%s agent_id=%s "
                "task_id=%s mode=%s — worker-bound agent returned 404 from "
                "the memory API; check tenant/agent config",
                bound_tenant, bound_agent, bound_task, mode,
            )
            return {"results": [], "ranking_used": mode}
        if response.status_code >= 400:
            raise MemoryToolError(
                f"Memory search failed with status {response.status_code}."
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise MemoryToolError(
                "Memory search returned non-JSON response."
            ) from exc

        # Pass through the API shape verbatim so the agent sees
        # ``ranking_used`` (silent hybrid→text degrade signal).
        results = payload.get("results", []) if isinstance(payload, dict) else []
        ranking_used = (
            payload.get("ranking_used") if isinstance(payload, dict) else mode
        )
        logger.info(
            "memory.search.served tenant_id=%s agent_id=%s task_id=%s "
            "mode_requested=%s ranking_used=%s result_count=%d",
            bound_tenant, bound_agent, bound_task,
            mode, ranking_used, len(results),
        )
        return {"results": results, "ranking_used": ranking_used}

    return StructuredTool.from_function(
        coroutine=memory_search,
        name="memory_search",
        description=MEMORY_SEARCH_DESCRIPTION,
        args_schema=MemorySearchArguments,
    )


# ---------------------------------------------------------------------------
# task_history_get
# ---------------------------------------------------------------------------


TASK_HISTORY_GET_DESCRIPTION = (
    "Fetch a bounded structured view of one past task in this same agent "
    "scope. Returns {task_id, agent_id, input, status, final_output, "
    "tool_calls, error_code, error_message, created_at, memory_id}. "
    "Each tool_calls entry is {name, args_preview, result_preview}; "
    f"previews are truncated to {TASK_HISTORY_PREVIEW_BYTES} bytes and "
    f"the list is capped at {TASK_HISTORY_TOOL_CALLS_CAP} items. "
    "Cross-agent or cross-tenant task ids return 'not found'."
)


_TASK_HISTORY_SQL = """
    SELECT
        t.task_id::text AS task_id,
        t.agent_id,
        t.input,
        t.status,
        t.output,
        t.last_error_code,
        t.last_error_message,
        t.created_at,
        m.memory_id::text AS memory_id
    FROM tasks t
    LEFT JOIN agent_memory_entries m
        ON m.task_id = t.task_id
        AND m.tenant_id = t.tenant_id
        AND m.agent_id = t.agent_id
    WHERE t.task_id = $1::uuid
      AND t.tenant_id = $2
      AND t.agent_id = $3
"""


def _extract_final_output(output_json: Any) -> str | None:
    """Pull a readable string out of ``tasks.output`` (JSONB dict or str)."""
    if output_json is None:
        return None
    if isinstance(output_json, str):
        try:
            output_json = json.loads(output_json)
        except Exception:
            return output_json
    if isinstance(output_json, dict):
        # Common shapes: {"response": "..."} or {"messages": [...]} — keep
        # the tool forgiving since Phase 1 and Phase 2 have stored different
        # shapes across tracks.
        for key in ("response", "final_output", "output", "result", "text"):
            val = output_json.get(key)
            if isinstance(val, str) and val.strip():
                return val
        # Best effort: serialize the dict.
        try:
            return json.dumps(output_json, default=str)
        except Exception:
            return str(output_json)
    return str(output_json)


def _build_task_history_get_tool(ctx: MemoryToolContext) -> StructuredTool:
    bound_tenant = ctx.tenant_id
    bound_agent = ctx.agent_id
    bound_task = ctx.task_id

    async def task_history_get(task_id: str) -> dict[str, Any]:
        # Scope binding: tenant_id + agent_id come from the worker context
        # and are appended server-side by the SQL WHERE clause. The LLM's
        # ``task_id`` argument is validated by asyncpg's uuid cast — a
        # malformed value raises, which we translate into a tool-shaped
        # "not found" so the graph stays in-loop.
        try:
            coro = _fetch_task_history_row(
                ctx.pool, task_id, bound_tenant, bound_agent
            )
            row = await _maybe_await_or_cancel(
                ctx, coro, operation="task_history_get"
            )
        except (asyncpg.exceptions.DataError, ValueError):
            # Malformed UUID. 404-not-403 — don't leak the parse error.
            logger.info(
                "memory.task_history.missed tenant_id=%s agent_id=%s "
                "caller_task_id=%s target_task_id=%s reason=invalid_uuid",
                bound_tenant, bound_agent, bound_task, task_id,
            )
            raise MemoryToolNotFoundError("Task not found.")

        if row is None:
            logger.info(
                "memory.task_history.missed tenant_id=%s agent_id=%s "
                "caller_task_id=%s target_task_id=%s",
                bound_tenant, bound_agent, bound_task, task_id,
            )
            raise MemoryToolNotFoundError("Task not found.")

        # Build the bounded structured view.
        input_str = _truncate(row["input"], TASK_HISTORY_INPUT_TRUNCATE_BYTES)
        final_output = _truncate(
            _extract_final_output(row["output"]),
            TASK_HISTORY_OUTPUT_TRUNCATE_BYTES,
        )

        # tool_calls: read from the LangGraph checkpointer. The adapter lives
        # in :mod:`tools.task_history_reader` so LangGraph field-name drift
        # needs a fix in exactly one place. Any deserialization failure
        # degrades to an empty list with a structured log — never crashes
        # the calling agent.
        tool_calls = await _read_tool_calls(
            ctx.checkpointer,
            task_id,
            cap=TASK_HISTORY_TOOL_CALLS_CAP,
            preview_bytes=TASK_HISTORY_PREVIEW_BYTES,
        )

        created_at = row["created_at"]
        created_at_iso = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        )

        result = {
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "input": input_str,
            "status": row["status"],
            "final_output": final_output,
            "tool_calls": tool_calls[:TASK_HISTORY_TOOL_CALLS_CAP],
            "error_code": row["last_error_code"],
            "error_message": row["last_error_message"],
            "created_at": created_at_iso,
            "memory_id": row["memory_id"],
        }

        logger.info(
            "memory.task_history.served tenant_id=%s agent_id=%s "
            "caller_task_id=%s target_task_id=%s status=%s has_memory=%s",
            bound_tenant, bound_agent, bound_task, task_id,
            row["status"], row["memory_id"] is not None,
        )
        return result

    return StructuredTool.from_function(
        coroutine=task_history_get,
        name="task_history_get",
        description=TASK_HISTORY_GET_DESCRIPTION,
        args_schema=TaskHistoryGetArguments,
    )


async def _fetch_task_history_row(
    pool: asyncpg.Pool,
    task_id: str,
    tenant_id: str,
    agent_id: str,
) -> asyncpg.Record | None:
    """Run the scope-bound SELECT against ``tasks`` + ``agent_memory_entries``.

    Factored out so tests can stub the pool with an object exposing
    ``fetchrow``. The SQL is a module-level constant so reviewers can verify
    both scope predicates (``tenant_id = $2 AND agent_id = $3``) are
    always present.
    """
    return await pool.fetchrow(_TASK_HISTORY_SQL, task_id, tenant_id, agent_id)


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def build_memory_tools(
    ctx: MemoryToolContext,
    *,
    stack_enabled: bool,
    auto_write: bool,
) -> list[StructuredTool]:
    """Assemble the per-task memory-tool list (Task 12 gate semantics).

    Gating:

    - ``memory_note`` and ``memory_search`` are returned whenever
      ``stack_enabled`` is True.
    - ``save_memory`` (Task 12) is returned only when ``stack_enabled`` is
      True AND ``auto_write`` is False — i.e., the ``agent_decides`` mode.
      In ``always`` mode the run writes unconditionally so the tool would be
      a no-op; in ``skip`` / memory-disabled mode the stack is off entirely.
    - ``task_history_get`` is returned unconditionally — diagnostic drill-
      down, scope-bound, safe even for memory-disabled agents.

    Returns a list of :class:`langchain_core.tools.StructuredTool` suitable for
    appending to the worker's built-in tool list before the BYOT merge in
    :func:`executor.graph.GraphExecutor._build_graph`.
    """
    tools: list[StructuredTool] = []
    if stack_enabled:
        tools.append(_build_memory_note_tool(ctx))
        tools.append(_build_memory_search_tool(ctx))
        if not auto_write:
            tools.append(_build_save_memory_tool(ctx))
    tools.append(_build_task_history_get_tool(ctx))
    return tools


__all__ = [
    "MEMORY_NOTE_DESCRIPTION",
    "MEMORY_NOTE_MAX_LEN",
    "MEMORY_SEARCH_DEFAULT_LIMIT",
    "MEMORY_SEARCH_DESCRIPTION",
    "MEMORY_SEARCH_TOOL_LIMIT_MAX",
    "MemoryNoteArguments",
    "MemorySearchArguments",
    "MemorySearchVectorUnavailableError",
    "MemoryToolContext",
    "MemoryToolError",
    "MemoryToolNotFoundError",
    "SAVE_MEMORY_DESCRIPTION",
    "SAVE_MEMORY_REASON_MAX_LEN",
    "SaveMemoryArguments",
    "TASK_HISTORY_GET_DESCRIPTION",
    "TaskHistoryGetArguments",
    "build_memory_tools",
]
