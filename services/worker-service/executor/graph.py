"""LangGraph executor for agent tasks.

Builds and executes the LangGraph state machine with the given agent configuration.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable

import asyncpg
import executor.providers as providers
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from langchain_core.tools import StructuredTool
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.errors import GraphRecursionError, GraphInterrupt
from langgraph.types import Command

from checkpointer.postgres import PostgresDurableCheckpointer, LeaseRevokedException
from core.config import WorkerConfig
from sandbox.provisioner import (
    SandboxProvisioner,
    SandboxProvisionError,
    SandboxConnectionError,
)

from executor.mcp_session import McpSessionManager, ToolServerConfig, McpConnectionError
from executor.schema_converter import mcp_tools_to_structured_tools, MAX_TOOLS_PER_AGENT
from executor import url_safety
from executor.compaction.state import RuntimeState
from executor.compaction.pre_model_hook import (
    HardFloorEvent,
    MemoryFlushFiredEvent,
    Tier3FiredEvent,
    Tier3SkippedEvent,
    compaction_pre_model_hook,
)
from executor.compaction.tokens import estimate_tokens as _estimate_tokens
from executor.compaction.summarizer import summarize_slice
from executor.memory_graph import (
    DEAD_LETTER_REASON_CANCELLED_BY_USER,
    DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE,
    MemoryDecision,
    MEMORY_WRITE_NODE_NAME,
    PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    SummarizerResult,
    build_attached_memories_preamble,
    build_pending_memory_dead_letter_template,
    checkpoint_tuple_has_prior_history,
    effective_memory_decision,
    memory_write_node,
)
from executor.embeddings import compute_embedding as _default_compute_embedding
from core.agent_runtime_state_repository import (
    decrement_running_count,
    increment_hour_window_cost,
)
from core.checkpoint_repository import (
    add_cost_and_preserve_metadata,
    add_cost_to_latest_terminal_checkpoint,
    fetch_latest_checkpoint_id,
    fetch_latest_terminal_checkpoint_id,
    set_cost_and_metadata,
    set_execution_metadata,
)
from core.conversation_log_repository import (
    ConversationLogRepository,
    compute_idempotency_key as _convlog_key,
)
from core.cost_ledger_repository import (
    insert_cost_row,
    min_created_at_in_hour_window,
    sum_hourly_cost_for_agent,
    sum_task_cost,
)
from core.memory_repository import (
    count_entries_for_agent,
    max_entries_for_agent,
    pending_memory_log_preview,
    read_memory_observations_by_task_id,
    read_pending_memory_from_state_values,
    resolve_attached_memories_for_task,
    trim_oldest,
    upsert_memory_entry,
)
from storage.s3_client import S3Client
from tools.definitions import (
    create_default_dependencies,
    WEB_SEARCH_TOOL,
    READ_URL_TOOL,
    DEV_SLEEP_TOOL,
    REQUEST_HUMAN_INPUT_TOOL,
    CREATE_TEXT_ARTIFACT_TOOL,
    SANDBOX_EXEC_TOOL,
    SANDBOX_READ_FILE_TOOL,
    SANDBOX_WRITE_FILE_TOOL,
    EXPORT_SANDBOX_FILE_TOOL,
    WebSearchArguments,
    ReadUrlArguments,
    DevSleepArguments,
    RequestHumanInputArguments,
    dev_task_controls_enabled,
    request_human_input,
)
from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxReadFileArguments,
    SandboxWriteFileArguments,
    ExportSandboxFileArguments,
    create_sandbox_exec_fn,
    create_sandbox_read_file_fn,
    create_sandbox_write_file_fn,
    create_export_sandbox_file_fn,
)
from tools.memory_tools import (
    MemoryToolContext,
    build_memory_tools,
)
from tools.errors import ToolExecutionError, ToolTransportError
from executor.mcp_session import McpToolCallError
from executor.compaction.defaults import OFFLOAD_THRESHOLD_BYTES
from executor.compaction.ingestion import (
    offload_ai_message_args,
    offload_tool_messages_batch,
)
from executor.compaction.tool_result_store import (
    S3ToolResultStore,
    ToolResultArtifactStore,
)
from executor.builtin_tools import (
    RECALL_TOOL_RESULT_NAME,
    RECALL_TOOL_RESULT_SYSTEM_PROMPT_HINT,
    build_recall_tool_result_tool,
)

import httpx

logger = logging.getLogger(__name__)

# Structlog logger for structured compaction events (e.g. compaction.per_result_capped).
# Uses core.logging.get_logger which returns a structlog BoundLogger.  We bind
# a placeholder worker_id here; the actual structured fields (tenant_id, agent_id,
# task_id) are bound at emit time via kwargs.
from core.logging import get_logger as _get_structlog_logger
_compaction_logger = _get_structlog_logger(worker_id="graph")


class _ContextExceededIrrecoverableError(Exception):
    """Internal sentinel: raised from agent_node when compaction's HardFloorEvent fires.

    The astream loop catches this and invokes ``_handle_dead_letter`` with
    ``reason=DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE``.  The name
    is prefixed with ``_`` to signal it is not part of the public API.
    """


def _handle_tool_error(e: Exception) -> str:
    """Route tool errors: re-raise infra failures for task-level retry,
    return user-fixable errors as messages so the LLM can self-correct."""
    if isinstance(e, (ToolTransportError, McpToolCallError)):
        raise e
    return f"Error: {e}\nPlease fix the error and try again."


def _apply_result_cap(tool_name: str, *, tenant_id: str, agent_id: str, task_id: str):
    """Back-compat no-op decorator (Track 7 Follow-up, Task 4).

    The legacy head+tail 25KB trim it used to apply has been replaced by
    S3-backed ingestion offload at the ``tools`` node boundary — see
    :func:`executor.compaction.ingestion.offload_tool_messages_batch` wired
    into ``_OffloadingToolNode`` below. Call sites are preserved as no-ops
    so that Task 3's pipeline rewrite can remove them during its touches of
    this file without blocking Task 4's ship.
    """

    def decorator(fn):
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Track 7 Task 13 — user-facing conversation log dual-write helpers.
#
# These helpers keep the append logic out of ``agent_node`` so the node
# stays readable.  Every function here is best-effort: all DB errors are
# swallowed inside :class:`ConversationLogRepository` itself (see its
# failure envelope).  None of these helpers ever raise.
# ---------------------------------------------------------------------------


def _convlog_origin_ref_for_message(msg: BaseMessage) -> str:
    """Return a stable origin_ref for a LangChain message.

    LangChain messages freshly constructed by the worker (e.g. a new
    ``HumanMessage(content=...)``) have ``msg.id = None``; we substitute
    ``f"seed:{uuid4()}"`` so the SHA-256 idempotency key is stable and
    distinct across messages.  The generated UUID is persisted on the
    message object (``msg.id``) so a super-step retry reuses the same
    message instance and therefore the same idempotency key — the
    ``ON CONFLICT DO NOTHING`` path then deduplicates correctly.
    """
    if getattr(msg, "id", None):
        return str(msg.id)
    seed = f"seed:{uuid.uuid4()}"
    try:
        msg.id = seed  # type: ignore[attr-defined]
    except Exception:
        # Defensive: if the underlying pydantic model forbids mutation,
        # fall through with the generated seed.  Retries will produce a
        # different key on this one message; ON CONFLICT then leaves the
        # first write in place as the canonical row.
        pass
    return seed


async def _convlog_append_pre_llm_turns(
    repo: ConversationLogRepository,
    *,
    task_id: str,
    tenant_id: str,
    checkpoint_id: str | None,
    messages: list[BaseMessage],
    last_super_step_message_count: int,
) -> None:
    """Append one entry per NEW ``HumanMessage`` / ``ToolMessage`` in this super-step.

    ``SystemMessage`` instances are intentionally excluded — they are
    platform or agent config, not user-facing conversation.  ``AIMessage``
    is also excluded here; it is appended separately after the LLM call.

    The slice is computed against ``state["messages"]`` (NOT the locally-
    prepended ``messages`` variable that may carry transient system
    prompts); the caller guarantees that.
    """
    new_slice = messages[last_super_step_message_count:]
    for msg in new_slice:
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, HumanMessage):
            origin_ref = _convlog_origin_ref_for_message(msg)
            key = _convlog_key(
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                origin_ref=origin_ref,
            )
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            await repo.append_entry(
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=checkpoint_id,
                idempotency_key=key,
                kind="user_turn",
                role="user",
                content={"text": text},
            )
        elif isinstance(msg, ToolMessage):
            origin_ref = _convlog_origin_ref_for_message(msg)
            key = _convlog_key(
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                origin_ref=origin_ref,
            )
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            # v1: we do not preserve the per-tool-result CapEvent on the
            # ToolMessage, so capped/orig_bytes are reported from the
            # post-cap text.  Phase 3+ blob-offload will carry cap
            # metadata explicitly.
            orig_bytes = len(text.encode("utf-8"))
            await repo.append_entry(
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=checkpoint_id,
                idempotency_key=key,
                kind="tool_result",
                role="tool",
                content={
                    "call_id": getattr(msg, "tool_call_id", "") or "",
                    "tool_name": getattr(msg, "name", "") or "",
                    "text": text,
                    "is_error": bool(getattr(msg, "status", "success") == "error"),
                },
                metadata={"orig_bytes": orig_bytes, "capped": False},
            )


async def _convlog_append_llm_response(
    repo: ConversationLogRepository,
    *,
    task_id: str,
    tenant_id: str,
    checkpoint_id: str | None,
    response: AIMessage,
) -> None:
    """Append one ``agent_turn`` for the AIMessage text + one ``tool_call`` per tool call."""
    msg_id = _convlog_origin_ref_for_message(response)
    text = response.content if isinstance(response.content, str) else ""
    agent_key = _convlog_key(
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        origin_ref=msg_id,
    )
    finish_reason = None
    try:
        rm = response.response_metadata or {}
        finish_reason = (
            rm.get("finish_reason")
            or rm.get("stop_reason")
            or None
        )
    except Exception:
        finish_reason = None
    await repo.append_entry(
        task_id=task_id,
        tenant_id=tenant_id,
        checkpoint_id=checkpoint_id,
        idempotency_key=agent_key,
        kind="agent_turn",
        role="assistant",
        content={"text": text or ""},
        metadata={"message_id": msg_id, "finish_reason": finish_reason},
    )

    for call in getattr(response, "tool_calls", None) or []:
        # ``call`` may be a dict (``{"name": ..., "args": ..., "id": ...}``)
        # or a LangChain ToolCall TypedDict — both expose key access.
        call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
        args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
        origin = call_id or f"{msg_id}:{name}"
        tc_key = _convlog_key(
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            origin_ref=f"toolcall:{origin}",
        )
        # json.dumps with default=str per spec §Content schema, applied
        # here so the append_entry payload is pure dict already.
        try:
            args_safe = json.loads(json.dumps(args, default=str))
        except Exception:
            args_safe = {"__unserializable__": str(args)}
        await repo.append_entry(
            task_id=task_id,
            tenant_id=tenant_id,
            checkpoint_id=checkpoint_id,
            idempotency_key=tc_key,
            kind="tool_call",
            role="assistant",
            content={
                "tool_name": name or "",
                "args": args_safe,
                "call_id": call_id or "",
            },
            metadata={"message_id": msg_id},
        )


async def _convlog_append_compaction_events(
    repo: ConversationLogRepository,
    *,
    task_id: str,
    tenant_id: str,
    checkpoint_id: str | None,
    events: list,
    summarized_through_before: int,
    summary_before: str,
    summary_after: str,
) -> None:
    """Append one entry per Tier3Fired / MemoryFlushFired event from the hook.

    Under the Track 7 Follow-up (Task 3) replace-and-rehydrate architecture
    the summary is REPLACED on each firing rather than appended to, so the
    conversation-log's ``summary_text`` is simply ``summary_after`` and we
    no longer compute an append delta. The prior ``summary_before`` is kept
    on the signature for future diff-style log entries (not used in v1).
    """
    # ``summary_before`` is retained in the signature as a forward-looking
    # hook but is intentionally unused — replace semantics means every firing
    # renders a fresh summary independent of prior content.
    _ = summary_before
    for ev in events:
        if isinstance(ev, Tier3FiredEvent):
            # Replace semantics — the entry's summary_text IS the new summary.
            summary_text = summary_after
            origin = f"tier3:{summarized_through_before}->{ev.new_summarized_through}"
            key = _convlog_key(
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                origin_ref=origin,
            )
            await repo.append_entry(
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=checkpoint_id,
                idempotency_key=key,
                kind="compaction_boundary",
                role="system",
                content={
                    "summary_text": summary_text,
                    "first_turn_index": int(summarized_through_before),
                    "last_turn_index": int(ev.new_summarized_through),
                },
                metadata={
                    "summarizer_model": ev.summarizer_model_id,
                    "turns_summarized": int(ev.new_summarized_through - summarized_through_before),
                    "summary_bytes": len(summary_text.encode("utf-8")),
                    "tokens_in": int(ev.tokens_in),
                    "tokens_out": int(ev.tokens_out),
                },
            )
        elif isinstance(ev, MemoryFlushFiredEvent):
            origin = f"flush:{checkpoint_id or 'init'}"
            key = _convlog_key(
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                origin_ref=origin,
            )
            await repo.append_entry(
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=checkpoint_id,
                idempotency_key=key,
                kind="memory_flush",
                role="system",
                content={},
                metadata={"fired_at_step": int(ev.fired_at_step)},
            )


async def _convlog_append_offload_emitted(
    repo: ConversationLogRepository,
    *,
    task_id: str,
    tenant_id: str,
    checkpoint_id: str | None,
    events: tuple,
    step_index: int,
) -> None:
    """Emit one ``offload_emitted`` entry per ingestion-offload pass.

    Track 7 Follow-up Task 5 contract: ONE entry per pass that offloaded
    ≥1 item — not one per item (the per-item form would spam the log on
    tasks with many small offloads). Payload is the roll-up:

        content  = {"count": N, "total_bytes": B, "step_index": S}

    ``step_index`` is the agent super-step index (approximated via the
    journal length so we don't need to thread the LangGraph step counter
    through the tool-node wrapper). All failures are swallowed by the
    repository's best-effort envelope.
    """
    success_events = [ev for ev in events if getattr(ev, "kind", "") == "success"]
    if not success_events:
        return
    total_bytes = sum(
        int(getattr(ev, "size_bytes", 0) or 0) for ev in success_events
    )
    count = len(success_events)
    # Deterministic idempotency-ref: the set of URIs uniquely identifies
    # this pass. Hashing inside ``_convlog_key`` (sha256) folds them into
    # the final key; we just need the material to be stable under retry.
    uri_material = "|".join(
        sorted(str(getattr(ev, "uri", "") or "") for ev in success_events)
    )
    origin = f"offload:{step_index}:{uri_material}"
    key = _convlog_key(
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        origin_ref=origin,
    )
    await repo.append_entry(
        task_id=task_id,
        tenant_id=tenant_id,
        checkpoint_id=checkpoint_id,
        idempotency_key=key,
        kind="offload_emitted",
        role="system",
        content={
            "count": count,
            "total_bytes": total_bytes,
            "step_index": int(step_index),
        },
    )


def _convlog_checkpoint_id_from_config(config: Any) -> str | None:
    """Best-effort checkpoint-id extraction from a LangGraph RunnableConfig.

    Falls back to ``None`` when the config shape is unexpected — the convlog
    repo substitutes the literal ``"init"`` for missing checkpoints so the
    idempotency hash remains stable.
    """
    try:
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
            ckpt = configurable.get("checkpoint_id")
            return str(ckpt) if ckpt is not None else None
    except Exception:
        return None
    return None


def _convlog_step_index_from_state(state: Any) -> int:
    """Return a stable super-step index for the current tool-node invocation.

    We use ``len(state["messages"])`` because the LangGraph step counter is
    not exposed in the RunnableConfig shape we receive here. Message-count-
    based indexing is coarser than a true step counter but monotone, stable
    under retry (idempotent key includes URIs so the retry dedups regardless),
    and sufficient for operator-facing ordering in the Console.
    """
    try:
        if isinstance(state, dict):
            msgs = state.get("messages")
            return len(msgs) if msgs is not None else 0
        msgs = getattr(state, "messages", None)
        return len(msgs) if msgs is not None else 0
    except Exception:
        return 0


def _extract_messages(out: Any):
    """Unpack a ToolNode's ``out`` into (messages_list, rewrap_fn).

    LangGraph ToolNode returns either ``{"messages": [...]}`` or a bare
    list; we normalise both into a list for downstream per-message work and
    return a callable that puts everything back into the original shape.
    """
    if isinstance(out, dict):
        msgs = list(out.get("messages") or [])

        def wrap(new_msgs: list) -> dict:
            return {**out, "messages": new_msgs}

        return msgs, wrap
    if isinstance(out, list):
        return list(out), lambda new_msgs: new_msgs
    # Unknown shape — pass through with no changes.
    return [], lambda _new: out


def _raw_tool_node_input_messages(state: Any) -> list:
    """Return ``state["messages"]`` in a way tolerant of either shape."""
    if isinstance(state, dict):
        return list(state.get("messages") or [])
    msgs = getattr(state, "messages", None)
    return list(msgs) if msgs is not None else []


def _recall_call_ids_from_state(state: Any) -> dict[str, str]:
    """Map ``tool_call_id → tool_name`` from the most recent AIMessage.

    Used to decide whether each ToolMessage emitted by the ToolNode
    corresponds to a ``recall_tool_result`` call — those need to bypass the
    ingestion offload path and get tagged so the compaction hook can
    recognise them later.
    """
    msgs = _raw_tool_node_input_messages(state)
    # Scan backward for the most recent AIMessage with tool_calls.
    for msg in reversed(msgs):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            out: dict[str, str] = {}
            for call in msg.tool_calls or []:
                call_id = call.get("id") if isinstance(call, dict) else None
                call_name = call.get("name") if isinstance(call, dict) else None
                if isinstance(call_id, str) and isinstance(call_name, str):
                    out[call_id] = call_name
            return out
    return {}


def _tag_recall_message(
    msg: ToolMessage, original_tool_call_id: str
) -> ToolMessage:
    """Return a copy of ``msg`` with ``recalled`` metadata applied.

    The tool surface itself returns a plain string (see
    ``executor.builtin_tools.recall_tool_result``); LangGraph's ToolNode
    wraps it into a ``ToolMessage`` with the call's id — we use THAT id as
    ``original_tool_call_id`` so the Option C replacement can point readers
    back at the right S3 artefact. The ToolMessage also carries a fresh id
    (assigned by LangGraph later) so add_messages replay semantics stay
    consistent.
    """
    existing_kwargs = dict(getattr(msg, "additional_kwargs", None) or {})
    existing_kwargs.setdefault("recalled", True)
    existing_kwargs.setdefault("original_tool_call_id", original_tool_call_id)
    return msg.model_copy(update={"additional_kwargs": existing_kwargs})


def _tag_recall_outputs_in_toolnode_output(out: Any, state_msgs: list) -> Any:
    """Pass-through variant for the offload-disabled branch.

    Even when offloading is off we still tag the recall-tool's ToolMessage
    so downstream compaction logic treats it the same way. Mirrors the
    enabled branch's tagging.
    """
    msgs, wrap = _extract_messages(out)
    if not msgs:
        return out
    # Build a map of tool_call_id → tool_name from the most recent AIMessage.
    name_by_id: dict[str, str] = {}
    for m in reversed(state_msgs):
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for call in m.tool_calls or []:
                call_id = call.get("id") if isinstance(call, dict) else None
                call_name = call.get("name") if isinstance(call, dict) else None
                if isinstance(call_id, str) and isinstance(call_name, str):
                    name_by_id[call_id] = call_name
            break
    new_msgs: list = []
    for m in msgs:
        if isinstance(m, ToolMessage):
            call_id = getattr(m, "tool_call_id", "") or ""
            if name_by_id.get(call_id) == RECALL_TOOL_RESULT_NAME:
                new_msgs.append(_tag_recall_message(m, call_id))
                continue
        new_msgs.append(m)
    return wrap(new_msgs)


def _reweave_messages(
    original: list,
    recall_msgs: list,
    offloaded_non_recall: list,
) -> list:
    """Recombine the two partitions while preserving the ToolNode's output order.

    ``original`` is the ToolNode output verbatim; ``recall_msgs`` are the
    recall-tagged ToolMessages (in ``original`` order); ``offloaded_non_recall``
    is the offload-rewritten list of everything else (also in order).
    """
    recall_iter = iter(recall_msgs)
    other_iter = iter(offloaded_non_recall)
    # Build a set of tool_call_ids that belong to the recall partition.
    recall_ids = {
        getattr(m, "tool_call_id", "") or ""
        for m in recall_msgs
        if isinstance(m, ToolMessage)
    }
    out: list = []
    for m in original:
        if isinstance(m, ToolMessage) and (
            (getattr(m, "tool_call_id", "") or "") in recall_ids
        ):
            try:
                out.append(next(recall_iter))
            except StopIteration:
                out.append(m)
        else:
            try:
                out.append(next(other_iter))
            except StopIteration:
                out.append(m)
    return out


class GraphExecutor:
    """Orchestrates LangGraph execution for a claimed task."""

    # Track 7 — fallback when a model is missing from the ``models`` table
    # or has a NULL ``context_window``. 128_000 is the platform floor: the
    # model-discovery service filters out sub-128K legacy models
    # (``DEACTIVATE_MODEL_IDS`` in services/model-discovery/main.py) before
    # they reach the table, so any active row is expected to support ≥128K.
    # A WARN below logs every fallback so operators can detect and fix
    # config holes (e.g. a brand-new provider model not yet in
    # ``CONTEXT_WINDOW_DEFAULTS``).
    _DEFAULT_MODEL_CONTEXT_WINDOW: int = 128_000

    def __init__(self, config: WorkerConfig, pool: asyncpg.Pool, deps=None, s3_client=None):
        self.config = config
        self.pool = pool
        self.deps = deps or create_default_dependencies()
        # Per-model cost rate cache: {model_name: (input_rate, output_rate)}
        self._cost_rate_cache: dict[str, tuple[int, int]] = {}
        if s3_client is not None:
            self.s3_client = s3_client
        else:
            s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL")
            s3_bucket_name = os.environ.get("S3_BUCKET_NAME", "platform-artifacts")
            self.s3_client = S3Client(
                endpoint_url=s3_endpoint_url,
                bucket_name=s3_bucket_name,
            )
        self._sandbox_provisioner: SandboxProvisioner | None = None

        # Phase 2 Track 5 Task 7: shared HTTP client for worker→Memory-API
        # calls (memory_search). Lazily created on first use so tests and
        # memory-disabled deployments never open a socket. Base URL comes
        # from ``MEMORY_API_BASE_URL`` env; the worker is co-located with
        # the API service in dev/compose so the default points at the
        # standard API port.
        self._memory_api_http_client: httpx.AsyncClient | None = None
        self._memory_api_base_url: str = os.environ.get(
            "MEMORY_API_BASE_URL", "http://localhost:8080"
        )

    def _get_memory_api_http_client(self) -> httpx.AsyncClient:
        """Lazily instantiate the worker-to-Memory-API HTTP client.

        One client per :class:`GraphExecutor` (connection-pool friendly);
        memory-disabled deployments never reach this path and therefore
        never open a socket. Exposed as a method so tests can swap the
        client in via attribute assignment on the executor instance.
        """
        if self._memory_api_http_client is None:
            # A short-ish timeout keeps a hung Memory API from wedging the
            # tool call. The worker's ``_await_or_cancel`` helper also
            # surfaces cancellations, but the httpx-level timeout is the
            # belt on top of suspenders.
            self._memory_api_http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
        return self._memory_api_http_client

    @property
    def sandbox_provisioner(self) -> SandboxProvisioner | None:
        """Lazy-initialize the sandbox provisioner (requires E2B_API_KEY)."""
        if self._sandbox_provisioner is None:
            api_key = os.environ.get("E2B_API_KEY")
            if api_key:
                self._sandbox_provisioner = SandboxProvisioner(api_key=api_key)
        return self._sandbox_provisioner

    async def _resolve_langfuse_credentials(self, endpoint_id: str) -> dict | None:
        """Query langfuse_endpoints table for credentials. Returns {host, public_key, secret_key} or None."""
        try:
            row = await self.pool.fetchrow(
                "SELECT host, public_key, secret_key FROM langfuse_endpoints WHERE endpoint_id = $1::uuid",
                endpoint_id,
            )
            if row is None:
                logger.warning("Langfuse endpoint %s not found in database", endpoint_id)
                return None
            host = row["host"]
            # Re-validate at trace time. The API blocks unsafe hosts on save, but a
            # DNS-based host saved as safe can be rebound to a metadata / internal
            # address before this worker ships traces + Basic Auth credentials to it.
            # Bail with None so the task still runs — tracing just degrades off.
            try:
                await url_safety.validate(host)
            except url_safety.UrlSafetyError as exc:
                logger.warning(
                    "Langfuse endpoint %s host rejected by url safety check; disabling tracing: %s",
                    endpoint_id, exc,
                )
                return None
            return {
                "host": host,
                "public_key": row["public_key"],
                "secret_key": row["secret_key"],
            }
        except Exception:
            logger.warning("Failed to resolve Langfuse credentials for endpoint %s", endpoint_id, exc_info=True)
            return None

    async def _lookup_tool_server_configs(
        self, conn, tenant_id: str, server_names: list[str]
    ) -> list[ToolServerConfig]:
        """Look up tool server configs from the database.

        Args:
            conn: asyncpg connection
            tenant_id: tenant ID
            server_names: list of server names from agent config

        Returns:
            List of ToolServerConfig objects

        Raises:
            McpConnectionError: if any server is not found or disabled
        """
        if not server_names:
            return []

        rows = await conn.fetch(
            """
            SELECT name, url, auth_type, auth_token, status
            FROM tool_servers
            WHERE tenant_id = $1 AND name = ANY($2)
            """,
            tenant_id,
            server_names,
        )

        found = {row["name"]: row for row in rows}

        configs = []
        for name in server_names:
            row = found.get(name)
            if row is None:
                raise McpConnectionError(
                    server_name=name,
                    server_url="unknown",
                    message=f"Tool server '{name}' not found in registry",
                )
            if row["status"] != "active":
                raise McpConnectionError(
                    server_name=name,
                    server_url=row["url"],
                    message=f"Tool server '{name}' is disabled",
                )
            configs.append(
                ToolServerConfig(
                    name=row["name"],
                    url=row["url"],
                    auth_type=row["auth_type"],
                    auth_token=row["auth_token"],
                )
            )

        return configs

    def _get_tools(
        self,
        allowed_tools: list[str],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        tenant_id: str = "default",
        agent_id: str = "unknown",
        sandbox=None,
        s3_client=None,
    ) -> list[StructuredTool]:
        # Shorthand: build a per-tool result wrapper bound to this task's
        # context. As of Track 7 Follow-up (Task 4), the decorator is a
        # no-op — tool-result byte bounding moved from head+tail trimming to
        # S3-backed ingestion offload, wired on the ToolNode output boundary
        # via ``_OffloadingToolNode`` below. The decorator call sites are
        # preserved so Task 3's pipeline rewrite can remove them cleanly.
        def _cap(name: str):
            return _apply_result_cap(
                name,
                tenant_id=tenant_id,
                agent_id=agent_id,
                task_id=task_id,
            )

        tools = []
        if "web_search" in allowed_tools:
            @_cap("web_search")
            async def web_search(query: str, max_results: int = 5):
                results = await self._await_or_cancel(
                    self.deps.search_provider.search(query, max_results),
                    cancel_event,
                    task_id=task_id,
                    operation="web_search",
                )
                return [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]
            tools.append(StructuredTool.from_function(
                coroutine=web_search,
                name="web_search",
                description=WEB_SEARCH_TOOL.description,
                args_schema=WebSearchArguments
            ))

        if "read_url" in allowed_tools:
            @_cap("read_url")
            async def read_url(url: str, max_chars: int = 5000):
                result = await self._await_or_cancel(
                    self.deps.read_url_fetcher.fetch(url, max_chars),
                    cancel_event,
                    task_id=task_id,
                    operation="read_url",
                )
                return {"final_url": result.final_url, "title": result.title, "content": result.content}
            tools.append(StructuredTool.from_function(
                coroutine=read_url,
                name="read_url",
                description=READ_URL_TOOL.description,
                args_schema=ReadUrlArguments
            ))

        if "request_human_input" in allowed_tools:
            @_cap("request_human_input")
            async def _request_human_input_capped(*args, **kwargs):
                # request_human_input is a sync function; wrap it for cap.
                return request_human_input(*args, **kwargs)
            tools.append(StructuredTool.from_function(
                coroutine=_request_human_input_capped,
                name="request_human_input",
                description=REQUEST_HUMAN_INPUT_TOOL.description,
                args_schema=RequestHumanInputArguments,
            ))

        if dev_task_controls_enabled() and "dev_sleep" in allowed_tools:
            @_cap("dev_sleep")
            async def dev_sleep(seconds: int = 10):
                await self._await_or_cancel(
                    asyncio.sleep(seconds),
                    cancel_event,
                    task_id=task_id,
                    operation="dev_sleep",
                )
                return {"slept_seconds": seconds}
            tools.append(StructuredTool.from_function(
                coroutine=dev_sleep,
                name="dev_sleep",
                description=DEV_SLEEP_TOOL.description,
                args_schema=DevSleepArguments
            ))

        # create_text_artifact is only offered when there is NO sandbox.
        # When a sandbox is available, the agent should use export_sandbox_file instead
        # to avoid sending file content through the LLM context window.
        has_sandbox = sandbox is not None and "export_sandbox_file" in allowed_tools
        if "create_text_artifact" in allowed_tools and not has_sandbox:
            from tools.upload_artifact import (
                CreateTextArtifactArguments,
                execute_create_text_artifact,
            )

            @_cap("create_text_artifact")
            async def create_text_artifact(
                filename: str,
                content: str,
                content_type: str = "text/plain",
            ):
                return await execute_create_text_artifact(
                    filename=filename,
                    content=content,
                    content_type=content_type,
                    s3_client=self.s3_client,
                    pool=self.pool,
                    task_id=task_id,
                    tenant_id=tenant_id,
                )

            tools.append(
                StructuredTool.from_function(
                    coroutine=create_text_artifact,
                    name="create_text_artifact",
                    description=CREATE_TEXT_ARTIFACT_TOOL.description,
                    args_schema=CreateTextArtifactArguments,
                )
            )

        # --- Sandbox tools (only when sandbox is provisioned) ---
        if sandbox is not None and "sandbox_exec" in allowed_tools:
            exec_fn = create_sandbox_exec_fn(sandbox)

            @_cap("sandbox_exec")
            async def sandbox_exec_wrapper(command: str):
                return await self._await_or_cancel(
                    exec_fn(command),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_exec",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_exec_wrapper,
                name="sandbox_exec",
                description=SANDBOX_EXEC_TOOL.description,
                args_schema=SandboxExecArguments,
            ))

        if sandbox is not None and "sandbox_read_file" in allowed_tools:
            read_fn = create_sandbox_read_file_fn(sandbox)

            @_cap("sandbox_read_file")
            async def sandbox_read_file_wrapper(path: str):
                return await self._await_or_cancel(
                    read_fn(path),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_read_file",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_read_file_wrapper,
                name="sandbox_read_file",
                description=SANDBOX_READ_FILE_TOOL.description,
                args_schema=SandboxReadFileArguments,
            ))

        if sandbox is not None and "sandbox_write_file" in allowed_tools:
            write_fn = create_sandbox_write_file_fn(sandbox)

            @_cap("sandbox_write_file")
            async def sandbox_write_file_wrapper(path: str, content: str):
                return await self._await_or_cancel(
                    write_fn(path, content),
                    cancel_event,
                    task_id=task_id,
                    operation="sandbox_write_file",
                )

            tools.append(StructuredTool.from_function(
                coroutine=sandbox_write_file_wrapper,
                name="sandbox_write_file",
                description=SANDBOX_WRITE_FILE_TOOL.description,
                args_schema=SandboxWriteFileArguments,
            ))

        if sandbox is not None and "export_sandbox_file" in allowed_tools and s3_client is not None:
            export_fn = create_export_sandbox_file_fn(
                sandbox,
                s3_client=s3_client,
                pool=self.pool,
                task_id=task_id,
                tenant_id=tenant_id,
            )

            @_cap("export_sandbox_file")
            async def export_sandbox_file_wrapper(path: str, filename: str | None = None):
                return await self._await_or_cancel(
                    export_fn(path, filename),
                    cancel_event,
                    task_id=task_id,
                    operation="export_sandbox_file",
                )

            tools.append(StructuredTool.from_function(
                coroutine=export_sandbox_file_wrapper,
                name="export_sandbox_file",
                description=EXPORT_SANDBOX_FILE_TOOL.description,
                args_schema=ExportSandboxFileArguments,
            ))

        return tools

    async def _build_graph(
        self,
        agent_config: dict[str, Any],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        tenant_id: str = "default",
        agent_id: str = "unknown",
        custom_tools: list[StructuredTool] | None = None,
        sandbox=None,
        s3_client=None,
        injected_files: list[str] | None = None,
        memory_decision: MemoryDecision | None = None,
        task_input: str | None = None,
        checkpointer: PostgresDurableCheckpointer | None = None,
        model_context_window: int = 128_000,
    ) -> StateGraph:
        """Assembles the LangGraph state machine and binds MCP tools."""
        provider = agent_config.get("provider", "anthropic")
        model_name = agent_config.get("model", "claude-3-5-sonnet-latest")
        temperature = agent_config.get("temperature", 0.7)
        allowed_tools = agent_config.get("allowed_tools", [])
        system_prompt = agent_config.get("system_prompt", "")
        sandbox_template = (agent_config.get("sandbox") or {}).get("template")

        # Track 7 Follow-up (Task 4/5) — resolve the ``offload_tool_results``
        # flag up front. Default ``true``; explicit ``false`` disables both
        # ingestion offload and Task 5's ``recall_tool_result`` registration
        # + system-prompt hint. The store is only constructed when the flag
        # is on so disabled deployments pay no boto3 / S3 overhead.
        _cm_cfg = agent_config.get("context_management") or {}
        _offload_flag = _cm_cfg.get("offload_tool_results")
        _offload_enabled: bool = True if _offload_flag is None else bool(_offload_flag)
        _offload_store: ToolResultArtifactStore | None = (
            S3ToolResultStore(self.s3_client) if _offload_enabled else None
        )

        # Build a separate platform system message with tool instructions
        platform_system_msg = self._build_platform_system_message(
            allowed_tools,
            injected_files=injected_files,
            sandbox_template=sandbox_template,
            memory_decision=memory_decision,
            offload_tool_results_enabled=_offload_enabled,
        )

        llm = await providers.create_llm(self.pool, provider, model_name, temperature)

        # Register built-in tools (pass sandbox and s3_client for sandbox tools)
        tools = self._get_tools(
            allowed_tools,
            cancel_event=cancel_event,
            task_id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            sandbox=sandbox,
            s3_client=s3_client,
        )

        # Phase 2 Track 5 Task 7 + Task 12 — memory tools are registered per-
        # task with (tenant_id, agent_id) bound from the worker's task context.
        # Scope is captured by closure; the LLM cannot override it via
        # arguments.
        #
        # - ``memory_note`` and ``memory_search`` are gated on
        #   ``decision.stack_enabled`` (agent.memory.enabled AND memory_mode
        #   ∈ {always, agent_decides}).
        # - ``save_memory`` (Task 12) is registered only in
        #   ``agent_decides`` mode (``stack_enabled=True AND auto_write=False``)
        #   — the agent's lever to opt this run in to writing a memory.
        # - ``task_history_get`` is always registered — diagnostic drill-down
        #   that is still safe cross-scope because of the bound predicate.
        decision = memory_decision or MemoryDecision(
            stack_enabled=False, auto_write=False
        )
        memory_tool_ctx = MemoryToolContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=task_id,
            pool=self.pool,
            memory_api_base_url=self._memory_api_base_url,
            http_client=self._get_memory_api_http_client(),
            cancel_event=cancel_event,
            await_or_cancel_fn=self._await_or_cancel,
            checkpointer=checkpointer,
        )
        # Track 7 Tier 0: helper to cap any StructuredTool whose coroutine is
        # not yet wrapped.  Used below for memory tools and MCP tools, which
        # are built outside _get_tools.
        def _wrap_tool_with_cap(structured_tool: StructuredTool) -> StructuredTool:
            """Return a copy of *structured_tool* whose coroutine is capped."""
            tool_nm = structured_tool.name
            original_coro = structured_tool.coroutine
            if original_coro is None:
                # Sync tool — wrap its func instead.
                original_func = structured_tool.func

                @_apply_result_cap(
                    tool_nm,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_id=task_id,
                )
                async def _capped_sync(*args, **kwargs):
                    return original_func(*args, **kwargs)

                return StructuredTool.from_function(
                    coroutine=_capped_sync,
                    name=structured_tool.name,
                    description=structured_tool.description or "",
                    args_schema=structured_tool.args_schema,
                )
            wrapped_coro = _apply_result_cap(
                tool_nm,
                tenant_id=tenant_id,
                agent_id=agent_id,
                task_id=task_id,
            )(original_coro)
            return StructuredTool.from_function(
                coroutine=wrapped_coro,
                name=structured_tool.name,
                description=structured_tool.description or "",
                args_schema=structured_tool.args_schema,
            )

        raw_memory_tools = build_memory_tools(
            memory_tool_ctx,
            stack_enabled=decision.stack_enabled,
            auto_write=decision.auto_write,
        )
        tools = tools + [_wrap_tool_with_cap(t) for t in raw_memory_tools]

        # Track 7 Follow-up Task 5 — register ``recall_tool_result`` when the
        # ingestion-offload flag is on. The tool is closure-bound over
        # ``(tenant_id, task_id, store)`` so the LLM cannot broaden scope or
        # point at a different tenant's artifacts. We DO NOT run it through
        # ``_wrap_tool_with_cap`` (that back-compat decorator is a no-op
        # under Task 4, but Task 5's output is explicitly exempt from Task
        # 4's ingestion offload anyway — see the special-case bypass in
        # ``tool_node`` below).
        if _offload_enabled and _offload_store is not None:
            tools.append(
                build_recall_tool_result_tool(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    store=_offload_store,
                )
            )

        # Merge custom tools from MCP servers — cap each one.
        if custom_tools:
            tools = tools + [_wrap_tool_with_cap(t) for t in custom_tools]

        # Enforce tool count limit
        if len(tools) > MAX_TOOLS_PER_AGENT:
            raise ValueError(
                f"Agent has {len(tools)} tools (max {MAX_TOOLS_PER_AGENT}). "
                f"Reduce the number of tool servers or use servers with fewer tools."
            )

        if tools:
            llm_with_tools = llm.bind_tools(tools)
        else:
            llm_with_tools = llm

        # Pool-backed adapter so the Tier-3 summariser can write a cost-ledger
        # row without knowing about asyncpg connection management.  Without
        # this, `task_context["cost_ledger"]` was None and the first Tier 3
        # firing raised AttributeError inside `summarize_slice`, which the
        # summariser treats as a fatal skip — permanently short-circuiting
        # Tier 3 for the rest of the task and forcing hard-floor dead-letters.
        _graph_pool = self.pool

        class _PoolBackedCostLedger:
            async def insert(
                self,
                *,
                tenant_id: str,
                agent_id: str,
                task_id: str,
                checkpoint_id: str | None,
                cost_microdollars: int,
                operation: str,
                model_id: str | None = None,
                tokens_in: int = 0,
                tokens_out: int = 0,
                summarized_through_turn_index_after: int | None = None,
            ) -> None:
                # The ledger column is TEXT NOT NULL. When LangGraph has not
                # yet assigned a checkpoint id (first super-step, or certain
                # retry paths), use a deterministic placeholder per firing so
                # the partial unique index on
                # (tenant_id, task_id, checkpoint_id, operation, summarized_through_turn_index_after)
                # still dedups crash-retries correctly.
                effective_ckpt = checkpoint_id or (
                    f"compaction:{summarized_through_turn_index_after}"
                    if summarized_through_turn_index_after is not None
                    else "compaction:unknown"
                )
                async with _graph_pool.acquire() as conn:
                    await insert_cost_row(
                        conn,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        task_id=task_id,
                        checkpoint_id=effective_ckpt,
                        cost_microdollars=cost_microdollars,
                        operation=operation,
                        model_id=model_id,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        summarized_through_turn_index_after=summarized_through_turn_index_after,
                    )

        _compaction_cost_ledger = _PoolBackedCostLedger()

        # `checkpoint_id` is resolved per-invocation from the RunnableConfig
        # inside agent_node, not captured here — see line below where
        # task_context is shallow-copied with the live value.
        task_context = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "checkpoint_id": None,
            "cost_ledger": _compaction_cost_ledger,
            "callbacks": [],
        }

        # Track 7 Task 13 — user-facing conversation-log repo.  Appends are
        # best-effort; all DB errors are swallowed inside the repo so a log
        # failure never fails the task.
        conversation_log_repo = ConversationLogRepository(self.pool)
        self._last_conversation_log_repo = conversation_log_repo

        async def agent_node(state: RuntimeState, config: RunnableConfig):
            # Track 7 Follow-up (Task 3) — the pre_model_hook owns system-
            # prompt placement and projection assembly, so we pass the raw
            # journal (``state["messages"]``) to the hook and let it assemble
            # the final ``[SystemMessage(system_prompt), SystemMessage(summary)?,
            # *middle, *keep_window]`` shape.
            _raw_state_messages = state["messages"]

            # Track 7 Task 13 — dual-write BEFORE compaction_pre_model_hook
            # fires so the conversation log holds the raw view of the super-
            # step's new turns (not the projection the LLM sees).
            _last_count = state.get("last_super_step_message_count", 0) or 0
            # Attempt to read a LangGraph-assigned checkpoint id off the
            # runnable config.  When absent (first turn / pre-Task-10),
            # append_entry substitutes the literal "init" via the shared
            # idempotency-key formula.
            _convlog_ckpt_id = None
            try:
                _convlog_ckpt_id = (
                    (config.get("configurable") or {}).get("checkpoint_id")
                    if isinstance(config, dict)
                    else None
                )
            except Exception:
                _convlog_ckpt_id = None

            await _convlog_append_pre_llm_turns(
                conversation_log_repo,
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=_convlog_ckpt_id,
                messages=_raw_state_messages,
                last_super_step_message_count=_last_count,
            )

            # Capture state values BEFORE compaction so we can compute the
            # compaction_boundary entry after the hook returns.
            _summary_before = state.get("summary", "") or ""
            _summarized_through_before = int(
                state.get("summarized_through_turn_index", 0) or 0
            )

            # Shallow-copy the shared task_context with the live checkpoint_id
            # so the summariser writes a cost-ledger row tagged to the right
            # checkpoint.  Falls back to None (adapter substitutes a
            # deterministic placeholder so the INSERT still dedups correctly).
            _per_call_task_context = {**task_context, "checkpoint_id": _convlog_ckpt_id}

            # Track 7 Follow-up (Task 3) — run the pre_model_hook before every
            # LLM call. The hook is pure w.r.t. the journal (never mutates
            # ``state["messages"]``); it returns the three-region projection
            # as ``pass_result.messages`` plus a ``state_updates`` dict.
            pass_result = await compaction_pre_model_hook(
                raw_messages=_raw_state_messages,
                state=state,
                agent_config=agent_config,
                model_context_window=model_context_window,
                task_context=_per_call_task_context,
                summarizer=summarize_slice,
                estimate_tokens_fn=lambda msgs: _estimate_tokens(msgs, provider=provider),
                system_prompt=system_prompt if system_prompt else None,
                platform_system_message=platform_system_msg if platform_system_msg else None,
            )
            messages_for_llm = pass_result.messages
            compaction_state_updates = pass_result.state_updates

            # Emit structured-log events from the hook and raise immediately
            # on HardFloorEvent — single pass avoids re-iterating the list.
            for ev in pass_result.events:
                if isinstance(ev, HardFloorEvent):
                    _compaction_logger.warning(
                        "compaction.hard_floor",
                        est_tokens=ev.est_tokens,
                        model_context_window=ev.model_context_window,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        task_id=task_id,
                    )
                    # Raise a sentinel exception so the astream loop can catch
                    # it and invoke _handle_dead_letter.
                    raise _ContextExceededIrrecoverableError(
                        f"Context window exceeded irrecoverably: "
                        f"{ev.est_tokens} tokens > {ev.model_context_window} window"
                    )
                elif isinstance(ev, Tier3FiredEvent):
                    _compaction_logger.info(
                        "compaction.tier3_fired",
                        summarizer_model_id=ev.summarizer_model_id,
                        tokens_in=ev.tokens_in,
                        tokens_out=ev.tokens_out,
                        new_summarized_through=ev.new_summarized_through,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        task_id=task_id,
                    )
                elif isinstance(ev, Tier3SkippedEvent):
                    _compaction_logger.info(
                        "compaction.tier3_skipped",
                        reason=ev.reason,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        task_id=task_id,
                    )

            # Track 7 Task 13 — mirror Tier3Fired / MemoryFlushFired events
            # into the user-facing conversation log.
            _summary_after = (
                compaction_state_updates.get("summary")
                if "summary" in compaction_state_updates
                else _summary_before
            ) or ""
            await _convlog_append_compaction_events(
                conversation_log_repo,
                task_id=task_id,
                tenant_id=tenant_id,
                checkpoint_id=_convlog_ckpt_id,
                events=pass_result.events,
                summarized_through_before=_summarized_through_before,
                summary_before=_summary_before,
                summary_after=_summary_after,
            )

            # Retry on rate limits inside the execution loop instead of
            # crashing and burning a task-level retry.
            max_rate_limit_retries = 5
            for attempt in range(max_rate_limit_retries + 1):
                try:
                    response = await self._await_or_cancel(
                        llm_with_tools.ainvoke(messages_for_llm, config),
                        cancel_event,
                        task_id=task_id,
                        operation="agent",
                    )
                    # Track 7 Follow-up (Task 4) — Tier 0 ingestion offload
                    # for oversized tool-call args on the AIMessage about to
                    # land in ``state["messages"]``. This is the arg-side
                    # counterpart to the ToolNode wrapper above. When the
                    # ``offload_tool_results`` config flag is off, this is a
                    # passthrough.
                    #
                    # Runs BEFORE the conversation-log append so the logged
                    # tool_call args reflect the reference-replaced view
                    # (the pre-offload values are still in S3 if Task 5's
                    # recall tool needs them).
                    if (
                        _offload_enabled
                        and _offload_store is not None
                        and isinstance(response, AIMessage)
                        and getattr(response, "tool_calls", None)
                    ):
                        _offload_outcome = await offload_ai_message_args(
                            response,
                            store=_offload_store,
                            tenant_id=tenant_id,
                            task_id=task_id,
                            threshold_bytes=OFFLOAD_THRESHOLD_BYTES,
                            log_context=_offload_log_ctx,
                        )
                        response = _offload_outcome.message  # type: ignore[assignment]
                        # Track 7 Follow-up Task 5 — one ``offload_emitted``
                        # convlog entry per AIMessage-arg-offload pass with
                        # ≥1 success. Aligns with the tool-result side in
                        # ``tool_node``.
                        await _convlog_append_offload_emitted(
                            conversation_log_repo,
                            task_id=task_id,
                            tenant_id=tenant_id,
                            checkpoint_id=_convlog_ckpt_id,
                            events=_offload_outcome.events,
                            step_index=len(_raw_state_messages),
                        )
                    # Track 7 Task 13 — append the agent's response and any
                    # tool-call requests to the user-facing conversation log
                    # BEFORE returning (and therefore before LangGraph
                    # commits the super-step's checkpoint).  Idempotency-key
                    # dedup handles retry-after-crash correctly.
                    await _convlog_append_llm_response(
                        conversation_log_repo,
                        task_id=task_id,
                        tenant_id=tenant_id,
                        checkpoint_id=_convlog_ckpt_id,
                        response=response,
                    )
                    return {"messages": [response], **compaction_state_updates}
                except Exception as e:
                    if self._is_rate_limit_error(e) and attempt < max_rate_limit_retries:
                        backoff = self._get_retry_after(e) or min(30, 5 * (2 ** attempt))
                        logger.warning(
                            "rate_limit_retry",
                            extra={
                                "task_id": task_id,
                                "attempt": attempt + 1,
                                "backoff_seconds": backoff,
                                "error": str(e)[:200],
                            },
                        )
                        await asyncio.sleep(backoff)
                        continue
                    raise

        # Define the Graph layout.
        # All tasks — memory-enabled and memory-disabled alike — use the
        # unified ``RuntimeState`` schema (Track 7 Task 2 refactor). The
        # ``stack_enabled`` flag still gates *topology* (whether the
        # ``memory_write`` node is wired and memory tools are registered);
        # only the *schema* is now unconditionally ``RuntimeState``.
        stack_enabled = decision.stack_enabled
        auto_write = decision.auto_write
        state_type = RuntimeState
        workflow = StateGraph(state_type)
        workflow.add_node("agent", agent_node, input_schema=state_type)

        # Wire the ``memory_write`` node whenever the stack is enabled.
        # Terminal path out of the agent runs through this node on the
        # branch selected by ``route_after_agent`` (below). HITL pauses,
        # budget pauses, and dead-letters exit the graph via different paths
        # and therefore never traverse this node.
        if stack_enabled:
            summarizer_model_id = (
                (agent_config.get("memory") or {}).get("summarizer_model")
                or PLATFORM_DEFAULT_SUMMARIZER_MODEL
            )
            summarizer_callable = self._build_summarizer_callable(
                default_model_id=summarizer_model_id,
            )
            embedding_callable = self._build_embedding_callable()

            async def memory_write_graph_node(state, config):
                return await memory_write_node(
                    state,
                    task_input=task_input,
                    summarizer_model_id=summarizer_model_id,
                    summarizer_callable=summarizer_callable,
                    embedding_callable=embedding_callable,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    config=config,
                )

            workflow.add_node(MEMORY_WRITE_NODE_NAME, memory_write_graph_node)
            workflow.add_edge(MEMORY_WRITE_NODE_NAME, END)

        # Phase 2 Track 5 Task 12 — unified routing function out of the
        # agent node. Replaces the pre-Task-12 ``tools_condition``-based
        # wiring so the decision tree is explicit:
        #
        # 1. Pending tool calls on the last AIMessage → ``tools`` (same as
        #    stock ``tools_condition``).
        # 2. ``auto_write`` OR the ``memory_opt_in`` state flag is True →
        #    ``memory_write`` (terminal memory branch).
        # 3. Otherwise → ``END`` (silent no-op in agent_decides-no-opt).
        def route_after_agent(state: Any) -> str:
            messages = state.get("messages") if isinstance(state, dict) else None
            if not messages:
                messages = getattr(state, "messages", None)
            last = messages[-1] if messages else None
            pending = bool(getattr(last, "tool_calls", None)) if last else False
            opt_in = bool(
                state.get("memory_opt_in", False)
                if isinstance(state, dict)
                else getattr(state, "memory_opt_in", False)
            )
            if pending:
                decision = "tools"
            elif stack_enabled and (auto_write or opt_in):
                decision = MEMORY_WRITE_NODE_NAME
            else:
                decision = END
            logger.info(
                "memory.route_after_agent task_id=%s decision=%s "
                "pending_tool_calls=%s stack_enabled=%s auto_write=%s opt_in=%s",
                task_id, decision, pending, stack_enabled, auto_write, opt_in,
            )
            return decision

        # Track 7 Follow-up (Task 4) — Tier 0 ingestion offload.
        #
        # ``_offload_enabled`` + ``_offload_store`` were resolved at the top
        # of this method (we pass the flag into the platform system-message
        # builder and close over the store for the recall tool). The
        # ToolNode wrapper and agent_node's AIMessage post-processing share
        # the same pair of values so the three offload sites cannot
        # disagree.
        _offload_log_ctx = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "task_id": task_id,
        }

        if tools:
            _raw_tool_node = ToolNode(tools, handle_tool_errors=_handle_tool_error)

            async def tool_node(state, config):
                """ToolNode wrapper that applies Tier 0 ingestion offload.

                Invokes the underlying ``ToolNode`` and, before the resulting
                ``ToolMessage`` list lands in ``state["messages"]``, routes
                any message whose ``content`` exceeds
                ``OFFLOAD_THRESHOLD_BYTES`` through
                :func:`offload_tool_messages_batch`. Below-threshold messages
                pass through verbatim. When
                ``context_management.offload_tool_results = false`` this
                wrapper is a trivial passthrough — no S3 writes.

                Track 7 Follow-up Task 5 — the ``recall_tool_result`` tool's
                own output BYPASSES this offload path. Re-offloading content
                the agent explicitly asked to see would create a re-read
                loop that defeats the purpose. The recall output is tagged
                with ``additional_kwargs={"recalled": True,
                "original_tool_call_id": ...}`` so the compaction hook's
                Option C replacement and the projection rule can recognise
                it later.
                """
                out = await _raw_tool_node.ainvoke(state, config)
                if not _offload_enabled or _offload_store is None:
                    # When the flag is off, still tag recall-tool outputs so
                    # the compaction hook's projection/Option-C logic can
                    # find them. Re-offload is already disabled in this
                    # branch.
                    out = _tag_recall_outputs_in_toolnode_output(
                        out, _raw_tool_node_input_messages(state)
                    )
                    return out
                # Split the ToolNode output into "recalled" (bypass offload,
                # tag additional_kwargs) and "other" (route through
                # offload_tool_messages_batch).
                msgs, wrap = _extract_messages(out)
                # Identify the calling AIMessage's tool_calls so we can tell
                # which ToolMessage corresponds to a ``recall_tool_result``
                # call. ToolNode preserves tool_call_id on each ToolMessage;
                # we match by id against the prior AIMessage's tool_calls.
                call_name_by_id = _recall_call_ids_from_state(state)
                recall_msgs: list[ToolMessage] = []
                other_msgs: list[ToolMessage] = []
                for m in msgs:
                    if not isinstance(m, ToolMessage):
                        other_msgs.append(m)  # type: ignore[arg-type]
                        continue
                    call_id = getattr(m, "tool_call_id", "") or ""
                    if call_name_by_id.get(call_id) == RECALL_TOOL_RESULT_NAME:
                        recall_msgs.append(_tag_recall_message(m, call_id))
                    else:
                        other_msgs.append(m)

                new_other, _events = await offload_tool_messages_batch(
                    other_msgs,
                    store=_offload_store,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    threshold_bytes=OFFLOAD_THRESHOLD_BYTES,
                    log_context=_offload_log_ctx,
                )

                # Best-effort ``offload_emitted`` convlog entry — one entry
                # per pass with ≥1 successful offload.
                await _convlog_append_offload_emitted(
                    conversation_log_repo,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    checkpoint_id=_convlog_checkpoint_id_from_config(config),
                    events=_events,
                    step_index=_convlog_step_index_from_state(state),
                )

                # Rebuild the list in the same order ``ToolNode`` produced —
                # the graph's downstream agent step assumes per-tool_call_id
                # pairing stability but does not depend on interleaving of
                # recall vs other messages (both land in the journal under
                # the same super-step).
                recombined = _reweave_messages(msgs, recall_msgs, new_other)
                return wrap(recombined)

            workflow.add_node("tools", tool_node)
            workflow.add_edge("tools", "agent")
            if stack_enabled:
                workflow.add_conditional_edges(
                    "agent",
                    route_after_agent,
                    {
                        "tools": "tools",
                        MEMORY_WRITE_NODE_NAME: MEMORY_WRITE_NODE_NAME,
                        END: END,
                    },
                )
            else:
                workflow.add_conditional_edges("agent", tools_condition)
        else:
            if stack_enabled:
                # No tools configured → same routing but without the
                # ``tools`` branch. ``route_after_agent`` still decides
                # between ``memory_write`` and ``END``.
                workflow.add_conditional_edges(
                    "agent",
                    route_after_agent,
                    {
                        MEMORY_WRITE_NODE_NAME: MEMORY_WRITE_NODE_NAME,
                        END: END,
                    },
                )
            else:
                workflow.add_edge("agent", END)

        workflow.add_edge(START, "agent")
        return workflow

    async def _get_model_cost_rates(self, model_name: str) -> tuple[int, int]:
        """Fetch input/output cost rates (microdollars per million tokens) from DB.
        Returns (input_rate, output_rate). Caches per model within a task execution."""
        if model_name in self._cost_rate_cache:
            return self._cost_rate_cache[model_name]

        try:
            row = await self.pool.fetchrow(
                "SELECT input_microdollars_per_million, output_microdollars_per_million FROM models WHERE model_id = $1",
                model_name,
            )
            if row is None:
                logger.warning("Model %s not found in models table; using zero cost rates", model_name)
                rates = (0, 0)
            else:
                rates = (
                    int(row["input_microdollars_per_million"] or 0),
                    int(row["output_microdollars_per_million"] or 0),
                )
        except Exception:
            logger.warning("Failed to fetch cost rates for model %s; using zero cost rates", model_name, exc_info=True)
            rates = (0, 0)

        self._cost_rate_cache[model_name] = rates
        return rates

    async def _get_model_context_window(self, model_name: str) -> int:
        """Fetch the context window token count for a model from the models table.

        Returns the model's ``context_window`` column value, or the
        platform floor (128_000) when the model is unknown.  Logs a
        structured warning when the default fires so operators can act.

        This value is resolved once at graph-build time (``execute_task``)
        and passed into ``_build_graph`` so ``compaction_pre_model_hook``
        always has a known-good value without an extra DB round-trip per
        LLM call.
        """
        try:
            row = await self.pool.fetchrow(
                "SELECT context_window FROM models WHERE model_id = $1",
                model_name,
            )
            if row is None or row["context_window"] is None:
                # WARN (not INFO): a fallback means the model isn't in the
                # ``models`` table or its ``context_window`` is NULL.
                # Discovery should populate this; if it doesn't, the model-
                # discovery service's CONTEXT_WINDOW_DEFAULTS / FALLBACKS
                # (services/model-discovery/main.py) need an entry, or the
                # agent is pointing at a model_id discovery never saw. A
                # config hole operators should fix — not a routine event.
                _compaction_logger.warning(
                    "compaction.model_context_window_unknown",
                    model=model_name,
                    fallback=self._DEFAULT_MODEL_CONTEXT_WINDOW,
                )
                return self._DEFAULT_MODEL_CONTEXT_WINDOW
            return int(row["context_window"])
        except Exception:
            logger.warning(
                "Failed to fetch context_window for model %s; using default %d",
                model_name,
                self._DEFAULT_MODEL_CONTEXT_WINDOW,
                exc_info=True,
            )
            return self._DEFAULT_MODEL_CONTEXT_WINDOW

    @staticmethod
    def _extract_tokens(metadata: dict) -> tuple[int, int]:
        """Returns (input_tokens, output_tokens). Falls back to (0, 0) if not found."""
        usage = (
            metadata.get("usage")              # Anthropic, Google
            or metadata.get("token_usage")     # OpenAI via LangChain
            or metadata.get("usage_metadata")  # Bedrock
            or {}
        )
        input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        return (int(input_t), int(output_t))

    async def _record_step_cost(
        self, conn, task_id: str, tenant_id: str, agent_id: str,
        checkpoint_id: str, cost_microdollars: int,
        execution_metadata: dict | None = None,
        *,
        worker_id: str,
    ) -> tuple:
        """Record step cost in a single transaction.

        Gated on the worker still owning the task lease. If the lease has been
        revoked or reassigned (heartbeat missed → scheduler evicted this worker)
        the function raises LeaseRevokedException without writing anything. Must
        be called inside an active transaction on `conn`.

        1. Validate lease (SELECT ... FOR UPDATE on tasks)
        2. Update checkpoints.cost_microdollars and execution_metadata for the given checkpoint_id
        3. INSERT into agent_cost_ledger
        4. UPSERT agent_runtime_state.hour_window_cost_microdollars (increment)
        5. Return (cumulative_task_cost, hourly_window_cost)
        """
        lease_ok = await conn.fetchval(
            '''SELECT 1 FROM tasks
               WHERE task_id = $1::uuid
                 AND tenant_id = $2
                 AND status = 'running'
                 AND lease_owner = $3
               FOR UPDATE''',
            task_id, tenant_id, worker_id,
        )
        if lease_ok is None:
            raise LeaseRevokedException(
                f"Lease revoked before cost write for task {task_id}"
            )

        await set_cost_and_metadata(
            conn,
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            cost_microdollars=cost_microdollars,
            execution_metadata=execution_metadata if execution_metadata else None,
        )

        # 2. Insert into agent_cost_ledger
        await insert_cost_row(
            conn,
            tenant_id=tenant_id,
            agent_id=agent_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            cost_microdollars=cost_microdollars,
        )

        # 3. Upsert agent_runtime_state, incrementing hour_window_cost_microdollars
        await increment_hour_window_cost(
            conn, tenant_id, agent_id, cost_microdollars
        )

        # 4. Return cumulative task cost and hourly window cost
        cumulative_task_cost = await sum_task_cost(conn, task_id)

        hourly_cost = await conn.fetchval(
            '''SELECT hour_window_cost_microdollars
               FROM agent_runtime_state
               WHERE tenant_id = $1 AND agent_id = $2''',
            tenant_id,
            agent_id,
        )

        return (int(cumulative_task_cost), int(hourly_cost or 0))

    async def _calculate_step_cost(self, response_metadata: dict, model_name: str) -> tuple[int, dict]:
        """Extract tokens from response metadata and calculate cost in microdollars.
        Returns (cost_microdollars, execution_metadata_dict)."""
        input_tokens, output_tokens = self._extract_tokens(response_metadata)
        input_rate, output_rate = await self._get_model_cost_rates(model_name)
        cost_microdollars = (input_tokens * input_rate + output_tokens * output_rate) // 1_000_000
        execution_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": model_name,
        }
        return (cost_microdollars, execution_metadata)

    async def _inject_input_files(self, sandbox, task_id: str, tenant_id: str) -> list[str]:
        """Download input artifacts from S3 and write them into the sandbox.

        Args:
            sandbox: E2B Sandbox instance
            task_id: UUID string
            tenant_id: tenant ID

        Returns:
            List of injected filenames (for system message generation)
        """
        # Query task_artifacts for input files
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT filename, s3_key, content_type, size_bytes
                   FROM task_artifacts
                   WHERE task_id = $1::uuid AND direction = 'input'
                   ORDER BY created_at""",
                task_id,
            )

        if not rows:
            return []

        injected_files = []
        for row in rows:
            filename = row["filename"]
            s3_key = row["s3_key"]
            size_bytes = row["size_bytes"]

            try:
                # Download from S3 via Track 1's S3Client (already async)
                data = await self.s3_client.download(s3_key)

                # Write into sandbox filesystem
                sandbox_path = f"/home/user/{filename}"
                await asyncio.to_thread(sandbox.files.write, sandbox_path, data)

                injected_files.append(filename)

                logger.info(
                    "input_file_injected",
                    extra={
                        "task_id": task_id,
                        "artifact_filename": filename,
                        "sandbox_path": sandbox_path,
                        "size_bytes": size_bytes,
                    },
                )

            except Exception as e:
                logger.error(
                    "input_file_injection_failed",
                    extra={
                        "task_id": task_id,
                        "artifact_filename": filename,
                        "s3_key": s3_key,
                        "error": str(e),
                    },
                )
                raise RuntimeError(
                    f"Failed to inject input file '{filename}' into sandbox: {str(e)}"
                ) from e

        logger.info(
            "input_files_injection_completed",
            extra={
                "task_id": task_id,
                "file_count": len(injected_files),
                "filenames": injected_files,
            },
        )

        return injected_files

    # --------------------------------------------------------------------
    # Phase 2 Track 5 — memory write path helpers
    # --------------------------------------------------------------------

    def _build_summarizer_callable(self, *, default_model_id: str):
        """Factory: returns an async callable that runs the summarizer LLM.

        The returned coroutine matches the :class:`SummarizerCallable`
        protocol expected by :func:`executor.memory_graph.memory_write_node`.
        It pulls credentials via :mod:`executor.providers` the same way the
        agent node does and reports tokens + cost in microdollars using the
        existing ``_calculate_step_cost`` path so cost accounting matches the
        rest of the worker.

        Summarizer retries ride on the provider SDK's own retry logic. If
        every retry fails the node switches to the template fallback.
        """
        async def summarizer(
            *, system: str, user: str, model_id: str
        ) -> SummarizerResult:
            effective_model = model_id or default_model_id
            provider = self._resolve_provider_for_model(effective_model)
            llm = await providers.create_llm(
                self.pool, provider, effective_model, temperature=0.2
            )
            # LangChain chat models accept a ``(role, content)`` tuple list
            # via ``ainvoke``. Two messages is enough: a system-shape hint
            # and the user payload carrying observations + trimmed
            # transcript.
            response = await llm.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )

            # Parse the expected ``TITLE:`` / ``SUMMARY:`` shape out of the
            # content; if the model deviates we fall back to a trimmed
            # single-line title + full body summary so the write still
            # succeeds cleanly.
            content = response.content if isinstance(response.content, str) else self._stringify_chat_content(response.content)
            title, summary = self._parse_summarizer_response(content)

            resp_meta = dict(getattr(response, "response_metadata", {}) or {})
            if getattr(response, "usage_metadata", None):
                resp_meta.setdefault("usage_metadata", response.usage_metadata)
            cost_microdollars, execution_metadata = await self._calculate_step_cost(
                resp_meta, effective_model
            )
            return SummarizerResult(
                title=title,
                summary=summary,
                model_id=effective_model,
                tokens_in=int(execution_metadata.get("input_tokens") or 0),
                tokens_out=int(execution_metadata.get("output_tokens") or 0),
                cost_microdollars=int(cost_microdollars or 0),
            )

        return summarizer

    def _build_embedding_callable(self):
        """Factory: returns the :func:`compute_embedding` closure bound to
        this worker's pool. Unit tests override this via monkey-patching; the
        injected argument to ``memory_write_node`` is the public extension
        point.
        """
        pool = self.pool

        async def embedding(text: str):
            return await _default_compute_embedding(text, pool=pool)

        return embedding

    @staticmethod
    def _resolve_provider_for_model(model_id: str) -> str:
        """Heuristic used by :func:`providers.create_llm` callers elsewhere
        in the worker: Anthropic Claude models by name prefix; everything
        else defaults to Bedrock (worker README § Model). The summarizer
        honours the same routing so an operator can set
        ``MEMORY_DEFAULT_SUMMARIZER_MODEL`` to a model configured under any
        existing provider credential.
        """
        if "claude" in model_id.lower():
            return "anthropic"
        return "bedrock"

    @staticmethod
    def _stringify_chat_content(content: Any) -> str:
        """Flatten the chat-model content list ``[{type: text, text: ...}]``
        into a single string. Anthropic returns content blocks, OpenAI plain
        strings; the summarizer parsing downstream works on a string.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _parse_summarizer_response(content: str) -> tuple[str, str]:
        """Parse the ``TITLE:`` / ``SUMMARY:`` convention from the summary
        prompt. Falls back to a first-line title + trimmed remainder if the
        model deviated. Empty title or summary triggers the fallback branch
        in the calling node via the "empty title/summary" guard.
        """
        lines = [line.rstrip() for line in content.splitlines() if line.strip()]
        title = ""
        summary_lines: list[str] = []
        mode: str | None = None
        for line in lines:
            lower = line.lstrip().lower()
            if lower.startswith("title:"):
                title = line.split(":", 1)[1].strip()
                mode = "after_title"
                continue
            if lower.startswith("summary:"):
                summary_lines.append(line.split(":", 1)[1].strip())
                mode = "summary"
                continue
            if mode == "summary":
                summary_lines.append(line)
            elif mode is None:
                # Model ignored the prompt format — use the first non-empty
                # line as title and everything after as summary.
                title = line.strip()
                mode = "summary"
        summary = "\n".join(filter(None, summary_lines)).strip()
        # Cap title at 200 chars (the DB CHECK constraint). Long titles
        # usually mean the model concatenated the prompt — clip politely
        # rather than crash the commit.
        if len(title) > 200:
            title = title[:197] + "..."
        return title, summary

    async def _commit_memory_and_complete_task(
        self,
        *,
        task_id: str,
        tenant_id: str,
        agent_id: str,
        pending_memory: dict[str, Any] | None,
        agent_config: dict[str, Any],
        output: dict[str, Any],
        worker_id: str,
    ) -> dict[str, Any]:
        """Co-commit the memory UPSERT and the lease-validated task UPDATE.

        Runs as ONE transaction:

        1. ``UPDATE tasks SET status='completed' ...`` guarded by
           ``lease_owner = :me`` — raises :class:`LeaseRevokedException` if
           the predicate fails, rolling back any memory write inside the
           same tx.
        2. UPSERT into ``agent_memory_entries`` keyed on ``task_id`` when
           ``pending_memory`` is non-``None``. The UPSERT returns
           ``(memory_id, inserted)`` — ``inserted`` distinguishes the INSERT
           from UPDATE branch.
        3. FIFO trim when the row count exceeds ``max_entries`` AND the
           UPSERT took the INSERT branch. UPDATE branch never trims.
        4. Summarizer / embedding cost ledger rows attributed to the
           task's most recent checkpoint (attribution parity with the
           chat-model per-step ledger writes).

        Returns a dict with observability keys the caller logs:
        ``{committed, memory_written, inserted, trim_evicted, memory_id}``.
        """
        log_extra = {
            "task_id": task_id,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
        }
        if pending_memory is None:
            logger.warning(
                "memory.write.missing_pending %s", log_extra
            )
        max_entries = max_entries_for_agent(agent_config)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Lease-validated task completion (FOR UPDATE pin). We
                # run the task UPDATE FIRST so the lease predicate fails
                # fast on eviction and the memory row never gets written.
                updated = await conn.fetchval(
                    '''UPDATE tasks
                       SET status='completed',
                           output=$1,
                           last_error_code=NULL,
                           last_error_message=NULL,
                           human_response=NULL,
                           version=version+1,
                           lease_owner=NULL,
                           lease_expiry=NULL
                       WHERE task_id=$2::uuid
                         AND status='running'
                         AND lease_owner=$3
                       RETURNING task_id''',
                    json.dumps(output),
                    task_id,
                    worker_id,
                )
                if updated is None:
                    raise LeaseRevokedException(
                        f"Lease revoked before memory commit for task {task_id}"
                    )

                inserted = False
                memory_id: Any = None
                trim_evicted = 0
                memory_written = False

                if pending_memory is not None:
                    entry = {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "title": pending_memory["title"],
                        "summary": pending_memory["summary"],
                        "observations": list(pending_memory.get("observations_snapshot") or []),
                        "outcome": pending_memory.get("outcome", "succeeded"),
                        "tags": list(pending_memory.get("tags") or []),
                        "content_vec": pending_memory.get("content_vec"),
                        "summarizer_model_id": pending_memory.get("summarizer_model_id"),
                    }
                    upserted = await upsert_memory_entry(conn, entry)
                    memory_id = upserted["memory_id"]
                    inserted = upserted["inserted"]
                    memory_written = True

                    if inserted:
                        post_insert_count = await count_entries_for_agent(
                            conn, tenant_id, agent_id
                        )
                        if post_insert_count > max_entries:
                            trim_evicted = await trim_oldest(
                                conn,
                                tenant_id=tenant_id,
                                agent_id=agent_id,
                                max_entries=max_entries,
                                keep_memory_id=memory_id,
                            )

                    # Cost ledger rows for summarizer + embedding — attributed
                    # to the task's most recent checkpoint. We resolve the
                    # checkpoint inside the transaction for a consistent read.
                    checkpoint_id = await fetch_latest_terminal_checkpoint_id(
                        conn, task_id
                    )
                    summarizer_cost = int(
                        pending_memory.get("summarizer_cost_microdollars") or 0
                    )
                    if checkpoint_id and summarizer_cost > 0:
                        await insert_cost_row(
                            conn,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            task_id=task_id,
                            checkpoint_id=checkpoint_id,
                            cost_microdollars=summarizer_cost,
                        )
                        # Hourly-spend accrues normally — memory cost is
                        # exempt from the per-task pause check ONLY, not
                        # from the rolling-window aggregation.
                        await increment_hour_window_cost(
                            conn, tenant_id, agent_id, summarizer_cost
                        )
                    embedding_cost = int(
                        pending_memory.get("embedding_cost_microdollars") or 0
                    )
                    # Embedding is zero-rated in v1; still record the ledger
                    # row when a real embedding was returned so the attribution
                    # metadata is visible to the API / Console.
                    if (
                        checkpoint_id
                        and pending_memory.get("content_vec") is not None
                    ):
                        await insert_cost_row(
                            conn,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            task_id=task_id,
                            checkpoint_id=checkpoint_id,
                            cost_microdollars=embedding_cost,
                        )

                    # Mirror the memory-write cost onto the checkpoint row.
                    # The API's cost totals and the per-step timeline read
                    # checkpoints.cost_microdollars (not agent_cost_ledger), so
                    # without this UPDATE the memory step shows $0 and the
                    # cumulative total doesn't advance between the agent's
                    # final response and the memory-saved step.
                    #
                    # This must be ADDITIVE, not a replacement. The sandbox
                    # cleanup path at the end of execute_task (see the
                    # "sandbox_cost_recording_failed" block) has already
                    # accumulated sandbox runtime spend onto this same
                    # checkpoint via `cost_microdollars = cost_microdollars
                    # + $1`; replacing would silently drop that spend from
                    # the timeline totals. Same goes for any future
                    # post-astream cost attribution path that lands on the
                    # terminal checkpoint. execution_metadata uses COALESCE
                    # so if an earlier writer populated it we don't clobber.
                    if checkpoint_id:
                        step_total_cost = summarizer_cost + embedding_cost
                        summarizer_tokens_in = int(
                            pending_memory.get("summarizer_tokens_in") or 0
                        )
                        summarizer_tokens_out = int(
                            pending_memory.get("summarizer_tokens_out") or 0
                        )
                        exec_metadata = {
                            "model": pending_memory.get("summarizer_model_id"),
                            "input_tokens": summarizer_tokens_in,
                            "output_tokens": summarizer_tokens_out,
                        }
                        await add_cost_and_preserve_metadata(
                            conn,
                            checkpoint_id=checkpoint_id,
                            task_id=task_id,
                            delta_microdollars=step_total_cost,
                            execution_metadata=exec_metadata,
                        )

                # Track 3: decrement running_task_count on completion.
                await decrement_running_count(conn, tenant_id, agent_id)
                await _insert_task_event(
                    conn, task_id, tenant_id, agent_id,
                    "task_completed", "running", "completed",
                    worker_id,
                )

        logger.info(
            "memory.write.committed task_id=%s inserted=%s trim_evicted=%d "
            "content_vec_null=%s preview=%s",
            task_id, inserted, trim_evicted,
            pending_memory.get("content_vec") is None if pending_memory else None,
            pending_memory_log_preview(pending_memory) if pending_memory else "null",
        )
        return {
            "committed": True,
            "memory_written": memory_written,
            "inserted": inserted,
            "trim_evicted": trim_evicted,
            "memory_id": memory_id,
        }

    def _build_platform_system_message(
        self,
        allowed_tools: list[str],
        *,
        injected_files: list[str] | None = None,
        sandbox_template: str | None = None,
        memory_decision: MemoryDecision | None = None,
        offload_tool_results_enabled: bool = True,
    ) -> str:
        """Build platform-generated system message with tool instructions.

        This is injected as a separate SystemMessage, hidden from the customer's
        system prompt — similar to how Claude Code injects system context.

        ``offload_tool_results_enabled`` (Track 7 Follow-up Task 5): when true,
        append a short directive telling the agent how to recognise the
        ingestion-offload placeholders and when to call ``recall_tool_result``.
        """
        sections = []

        sections.append(f"Today's date is {datetime.now(timezone.utc).strftime('%Y-%m-%d')}.")

        if "request_human_input" in allowed_tools:
            sections.append(
                "You have access to a `request_human_input` tool. "
                "When you need clarification, additional information, or approval from the user, "
                "you MUST call the `request_human_input` tool instead of writing questions in your response. "
                "This will pause execution and wait for the user to respond."
            )

        sandbox_tools = {"sandbox_exec", "sandbox_read_file", "sandbox_write_file", "export_sandbox_file"}
        if sandbox_tools.intersection(allowed_tools):
            template_note = f" running the `{sandbox_template}` environment" if sandbox_template else ""
            sections.append(
                f"You have access to a sandbox environment{template_note} for code execution. "
                "Use `sandbox_exec` to run shell commands, `sandbox_write_file` to create files, "
                "`sandbox_read_file` to read files, and `export_sandbox_file` to save files as output artifacts. "
                "Write code to files first, then execute them with sandbox_exec."
            )

        if "create_text_artifact" in allowed_tools and not sandbox_tools.intersection(allowed_tools):
            sections.append(
                "You can save output files using the `create_text_artifact` tool. "
                "Use this to produce reports, data files, or other deliverables."
            )

        if "web_search" in allowed_tools:
            sections.append(
                "You can search the web using the `web_search` tool for up-to-date information."
            )

        if "read_url" in allowed_tools:
            sections.append(
                "You can read web pages using the `read_url` tool to fetch content from URLs."
            )

        if injected_files:
            file_list = "\n".join(f"  - /home/user/{f}" for f in injected_files)
            sections.append(
                f"The following input files have been provided and are available "
                f"in the sandbox filesystem:\n{file_list}\n"
                f"You can read these files using sandbox_read_file or process them "
                f"with sandbox_exec commands."
            )

        # Phase 2 Track 5 Task 12 — memory-tool framing. Gated on what is
        # actually registered: ``memory_note`` / ``memory_search`` whenever
        # the stack is on; ``save_memory`` only in ``agent_decides``. Tool
        # descriptions alone underspecify behavior — Anthropic / OpenAI /
        # Bedrock LLMs reliably forget optional retrieval tools without a
        # platform nudge, and ``save_memory`` in particular needs the
        # opt-in framing to preserve ``agent_decides`` semantics (a MUST
        # directive would collapse it into ``always`` mode).
        if memory_decision is not None and memory_decision.stack_enabled:
            sections.append(
                "This agent has persistent memory. Before starting non-trivial "
                "work, consider calling `memory_search` to recall relevant past "
                "runs. During the run, use `memory_note` to capture salient "
                "intermediate findings that should survive into the final "
                "memory entry."
            )
            if not memory_decision.auto_write:
                sections.append(
                    "Memory writes are opt-in for this run: call "
                    "`save_memory(reason=...)` exactly when the run has "
                    "produced something worth remembering (non-trivial "
                    "findings, customer decisions, recurring patterns). "
                    "Skip the call for routine or trivial runs — the absence "
                    "of a call means no memory entry is written."
                )

        # Track 7 Follow-up Task 5 — ingestion-offload directive. Appended
        # only when the feature is on so agents running with the flag off
        # don't see references to a tool they won't find in their tool
        # list.
        if offload_tool_results_enabled:
            sections.append(RECALL_TOOL_RESULT_SYSTEM_PROMPT_HINT)

        return "\n\n".join(sections)

    async def execute_task(self, task_data: dict[str, Any], cancel_event: asyncio.Event) -> None:
        """Main entrypoint from the executor router."""
        task_id = str(task_data["task_id"])
        tenant_id = task_data["tenant_id"]
        agent_config = json.loads(task_data["agent_config_snapshot"])
        task_input = task_data["input"]
        max_steps = task_data.get("max_steps", 100)
        task_timeout_seconds = task_data.get("task_timeout_seconds", 3600)
        worker_id = self.config.worker_id
        agent_id = task_data.get("agent_id") or "unknown"

        # Phase 2 Track 5 Task 12: single-source-of-truth memory gate —
        # computed once and consulted by graph assembly, the commit path,
        # and the budget carve-out. ``memory_mode`` is the typed task column
        # introduced in migration 0012 and replaces the legacy
        # ``skip_memory_write`` boolean. Default is ``always`` (today's
        # memory-enabled behaviour) so a payload without the field preserves
        # the pre-Task-12 default for memory-enabled agents.
        memory_mode = task_data.get("memory_mode", "always")
        if not isinstance(memory_mode, str):
            memory_mode = "always"
        memory_decision = effective_memory_decision(
            agent_config=agent_config,
            memory_mode=memory_mode,
        )
        memory_enabled_for_task = memory_decision.stack_enabled

        # Reset per-task cost rate cache
        self._cost_rate_cache = {}

        # Resolve per-task Langfuse credentials
        langfuse_credentials: dict | None = None
        per_task_langfuse_client: Langfuse | None = None
        langfuse_endpoint_id = task_data.get("langfuse_endpoint_id")
        if langfuse_endpoint_id:
            try:
                creds = await self._resolve_langfuse_credentials(str(langfuse_endpoint_id))
                if creds:
                    client = Langfuse(
                        public_key=creds["public_key"],
                        secret_key=creds["secret_key"],
                        host=creds["host"],
                    )
                    if client.auth_check():
                        per_task_langfuse_client = client
                        langfuse_credentials = creds
                    else:
                        logger.warning(
                            "Langfuse auth check failed for task %s endpoint %s, continuing without traces",
                            task_id, langfuse_endpoint_id,
                        )
            except Exception:
                logger.warning(
                    "Langfuse initialization failed for task %s, continuing without traces",
                    task_id, exc_info=True,
                )

        # Extract tool_servers from agent config
        tool_server_names = agent_config.get("tool_servers", [])
        if not isinstance(tool_server_names, list) or not all(isinstance(n, str) for n in tool_server_names):
            logger.error("invalid_tool_servers_config", extra={"task_id": task_id, "tool_servers": tool_server_names})
            tool_server_names = []

        session_manager: McpSessionManager | None = None
        custom_tools: list[StructuredTool] = []
        sandbox = None
        provisioner = None
        # Initialized below in the try block; predeclared so the
        # ``except`` handlers can pass it into ``_handle_dead_letter`` even
        # when a pre-graph failure (tool servers, sandbox) triggers dead-
        # letter before the checkpointer is wired up.
        checkpointer: PostgresDurableCheckpointer | None = None

        try:
            # Look up and connect to MCP tool servers if configured
            if tool_server_names:
                dead_letter_info = None
                async with self.pool.acquire() as conn:
                    try:
                        server_configs = await self._lookup_tool_server_configs(
                            conn, tenant_id, tool_server_names
                        )
                    except McpConnectionError as e:
                        logger.error(
                            "tool_server_unavailable",
                            extra={
                                "task_id": task_id,
                                "server_name": e.server_name,
                                "server_url": e.server_url,
                                "error": str(e),
                            },
                        )
                        dead_letter_info = {
                            "reason": "non_retryable_error",
                            "error_msg": str(e),
                            "error_code": "tool_server_unavailable",
                        }

                if dead_letter_info:
                    await self._handle_dead_letter(
                        task_id, tenant_id, agent_id, **dead_letter_info
                    )
                    return

                session_manager = McpSessionManager()
                try:
                    tools_by_server = await session_manager.connect(server_configs)
                except McpConnectionError as e:
                    logger.error(
                        "tool_server_unavailable",
                        extra={
                            "task_id": task_id,
                            "server_name": e.server_name,
                            "server_url": e.server_url,
                            "error": str(e),
                        },
                    )
                    await self._handle_dead_letter(
                        task_id,
                        tenant_id,
                        agent_id,
                        reason="non_retryable_error",
                        error_msg=str(e),
                        error_code="tool_server_unavailable",
                    )
                    return

                # Convert MCP tool schemas to StructuredTool objects
                for server_name, tool_schemas in tools_by_server.items():
                    server_tools = mcp_tools_to_structured_tools(
                        server_name=server_name,
                        tool_schemas=tool_schemas,
                        call_fn=session_manager.call_tool,
                        cancel_event=cancel_event,
                        await_or_cancel_fn=self._await_or_cancel,
                        task_id=task_id,
                    )
                    custom_tools.extend(server_tools)

                logger.info(
                    "custom_tools_discovered",
                    extra={
                        "task_id": task_id,
                        "server_count": len(tools_by_server),
                        "tool_count": len(custom_tools),
                    },
                )

            # --- Sandbox provisioning ---
            sandbox_config = agent_config.get("sandbox") or {}
            sandbox_enabled = sandbox_config.get("enabled", False)
            sandbox = None
            sandbox_start_time = None
            injected_files: list[str] = []
            provisioner = None

            if sandbox_enabled:
                provisioner = self.sandbox_provisioner
                if provisioner is None:
                    logger.error(
                        "sandbox_provisioner_unavailable",
                        extra={"task_id": task_id},
                    )
                    await self._handle_dead_letter(
                        task_id, tenant_id, agent_id,
                        reason="sandbox_provision_failed",
                        error_msg="E2B_API_KEY not configured. Cannot provision sandbox.",
                        error_code="sandbox_provision_failed",
                    )
                    return

                existing_sandbox_id = task_data.get("sandbox_id")

                if existing_sandbox_id:
                    # Crash recovery: reconnect to existing sandbox
                    try:
                        sandbox = await provisioner.connect(existing_sandbox_id)
                        logger.info(
                            "sandbox_crash_recovery_success",
                            extra={
                                "task_id": task_id,
                                "sandbox_id": existing_sandbox_id,
                            },
                        )
                    except SandboxConnectionError as e:
                        logger.warning(
                            "sandbox_crash_recovery_failed",
                            extra={
                                "task_id": task_id,
                                "sandbox_id": existing_sandbox_id,
                                "error": str(e),
                            },
                        )
                        await self._handle_dead_letter(
                            task_id, tenant_id, agent_id,
                            reason="sandbox_lost",
                            error_msg=f"Sandbox '{existing_sandbox_id}' is no longer available: {str(e)}",
                            error_code="sandbox_lost",
                        )
                        return
                    # Files already present in sandbox from prior run; do not overwrite.
                    injected_files = []
                else:
                    # Fresh provision
                    template = sandbox_config.get("template", "base")
                    vcpu = sandbox_config.get("vcpu", 2)
                    memory_mb = sandbox_config.get("memory_mb", 2048)
                    timeout_seconds = sandbox_config.get("timeout_seconds", 3600)

                    try:
                        sandbox = await provisioner.provision(
                            template=template,
                            vcpu=vcpu,
                            memory_mb=memory_mb,
                            timeout_seconds=timeout_seconds,
                        )
                    except SandboxProvisionError as e:
                        logger.error(
                            "sandbox_provision_exhausted",
                            extra={
                                "task_id": task_id,
                                "template": template,
                                "error": str(e),
                            },
                        )
                        await self._handle_dead_letter(
                            task_id, tenant_id, agent_id,
                            reason="sandbox_provision_failed",
                            error_msg=str(e),
                            error_code="sandbox_provision_failed",
                        )
                        return

                    # Store sandbox_id in DB immediately after provisioning
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE tasks SET sandbox_id = $1 WHERE task_id = $2::uuid",
                            sandbox.sandbox_id,
                            task_id,
                        )

                    logger.info(
                        "sandbox_id_persisted",
                        extra={
                            "task_id": task_id,
                            "sandbox_id": sandbox.sandbox_id,
                        },
                    )

                    # Inject input files only on fresh provision; on crash recovery
                    # the sandbox already has the files (possibly modified by the agent).
                    injected_files = await self._inject_input_files(sandbox, task_id, tenant_id)

                sandbox_start_time = time.monotonic()

            # 2. Init checkpointer
            checkpointer = PostgresDurableCheckpointer(
                self.pool,
                worker_id=worker_id,
                tenant_id=tenant_id
            )

            # Track 7 Task 8 — resolve model context window once at graph-build
            # time (not per LLM call) and cache it for the lifetime of this
            # execute_task invocation.  Passed through to the pre_model_hook
            # via _build_graph so agent_node always has the right value.
            _model_name_for_ctx = agent_config.get("model", "claude-3-5-sonnet-latest")
            _model_context_window = await self._get_model_context_window(_model_name_for_ctx)

            # 3. Build & Compile graph
            graph = await self._build_graph(
                agent_config,
                cancel_event=cancel_event,
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                custom_tools=custom_tools if custom_tools else None,
                sandbox=sandbox,
                s3_client=self.s3_client,
                injected_files=injected_files if sandbox_enabled else None,
                memory_decision=memory_decision,
                task_input=task_input,
                checkpointer=checkpointer,
                model_context_window=_model_context_window,
            )
            compiled_graph = graph.compile(checkpointer=checkpointer)

            # 4. Config map
            config = self._build_runnable_config(
                task_id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                max_steps=max_steps,
                langfuse_credentials=langfuse_credentials,
            )

            async def run_astream():
                nonlocal session_manager, per_task_langfuse_client, sandbox
                # For first run, inject HumanMessage based on initial input.
                # ``first_execution`` is the single predicate used to gate
                # initial-state setup: True when there is no checkpoint tuple
                # at all AND when a checkpoint tuple exists but has no prior
                # messages. (LangGraph durability can persist an empty
                # checkpoint before the first super-step in some modes; both
                # scenarios want identical first-run handling.)
                checkpoint_tuple = await checkpointer.aget_tuple(config)
                has_prior_history = checkpoint_tuple_has_prior_history(
                    checkpoint_tuple
                )
                first_execution = not has_prior_history

                # Phase 2 Track 5 Task 8 — resolve attached memories (first
                # execution only) + seed observations from the existing memory
                # row (first-execution-with-memory-row, i.e. redrive after a
                # prior dead-letter template write). Attachments are
                # immutable after task creation, so we only resolve on first
                # execution; the injected preamble is captured in the first
                # super-step checkpoint and implicit for subsequent resumes.
                attached_preamble: str | None = None
                seeded_observations: list[str] | None = None
                if first_execution:
                    async with self.pool.acquire() as _attach_conn:
                        if memory_enabled_for_task:
                            seeded_observations = (
                                await read_memory_observations_by_task_id(
                                    _attach_conn, tenant_id, agent_id, task_id,
                                )
                            )
                        # Attach injection runs regardless of
                        # ``effective_memory_enabled`` — see design doc §
                        # "Read Path → Retrieval is always explicit". Console
                        # gates this UI-side when the agent has memory
                        # disabled; the worker does not additionally gate.
                        resolved_entries = (
                            await resolve_attached_memories_for_task(
                                _attach_conn, tenant_id, agent_id, task_id,
                            )
                        )
                    attached_preamble = build_attached_memories_preamble(
                        resolved_entries
                    )
                    if attached_preamble is not None:
                        approx_bytes = len(
                            attached_preamble.encode("utf-8", errors="replace")
                        )
                        logger.info(
                            "memory.attach.injected tenant_id=%s agent_id=%s "
                            "task_id=%s count=%d approx_bytes=%d",
                            tenant_id, agent_id, task_id,
                            len(resolved_entries), approx_bytes,
                        )
                    if seeded_observations:
                        logger.info(
                            "memory.seeding.applied tenant_id=%s agent_id=%s "
                            "task_id=%s observation_count=%d",
                            tenant_id, agent_id, task_id,
                            len(seeded_observations),
                        )

                # Build initial message list. Attachment preamble (when
                # present) is prepended as a SystemMessage BEFORE the agent's
                # own system prompt — the distinction between "agent rules"
                # and "customer-attached memory" must be preserved. Because
                # the ``agent_node`` only auto-synthesizes system prompts
                # when NO SystemMessage is present in state, we explicitly
                # include the agent's system prompt + platform system
                # message here when we're also injecting the preamble, so
                # those aren't silently dropped on the first super-step.
                initial_messages: list[Any] = []
                if attached_preamble is not None:
                    initial_messages.append(
                        SystemMessage(
                            content=(
                                "The following memory entries have been "
                                "attached to this task by the customer. "
                                "Use them as reference context:\n\n"
                                f"{attached_preamble}"
                            )
                        )
                    )
                    # Reconstruct the agent's system prompts that
                    # ``agent_node`` would normally synthesise on the first
                    # super-step — see agent_node's "no SystemMessage"
                    # branch in _build_graph.
                    agent_system_prompt = agent_config.get("system_prompt", "")
                    allowed_tools_for_sys = agent_config.get("allowed_tools", [])
                    sandbox_template_for_sys = (
                        agent_config.get("sandbox") or {}
                    ).get("template")
                    # Track 7 Follow-up Task 5 — mirror the graph-build-time
                    # resolution so the initial platform SystemMessage shows
                    # (or hides) the ``recall_tool_result`` hint consistently
                    # with the tool registration gate in ``_build_graph``.
                    _cm_cfg_for_sys = (
                        agent_config.get("context_management") or {}
                    )
                    _offload_flag_for_sys = _cm_cfg_for_sys.get(
                        "offload_tool_results"
                    )
                    _offload_enabled_for_sys = (
                        True if _offload_flag_for_sys is None
                        else bool(_offload_flag_for_sys)
                    )
                    platform_system_msg = self._build_platform_system_message(
                        allowed_tools_for_sys,
                        injected_files=(
                            injected_files if sandbox_enabled else None
                        ),
                        sandbox_template=sandbox_template_for_sys,
                        memory_decision=memory_decision,
                        offload_tool_results_enabled=_offload_enabled_for_sys,
                    )
                    if agent_system_prompt:
                        initial_messages.append(
                            SystemMessage(content=agent_system_prompt)
                        )
                    if platform_system_msg:
                        initial_messages.append(
                            SystemMessage(content=platform_system_msg)
                        )
                if first_execution:
                    initial_messages.append(HumanMessage(content=task_input))

                initial_input: Any
                if first_execution:
                    # Track 7 Task 2 — seed ALL RuntimeState fields with
                    # reducer-safe defaults so every task graph starts from a
                    # known-good state regardless of memory stack enablement.
                    # LangGraph tolerates extra keys; memory-disabled tasks
                    # simply never overwrite these fields.
                    _observations: list[str] = (
                        list(seeded_observations)
                        if memory_enabled_for_task and seeded_observations
                        else []
                    )
                    _payload: dict[str, Any] = {
                        "messages": initial_messages,
                        "observations": _observations,
                        "pending_memory": {},
                        # Phase 2 Track 5 Task 12 — per-run reset of the
                        # ``agent_decides`` opt-in flag. The field has no
                        # reducer (last-write-wins), so seeding ``False`` here
                        # guarantees the agent must re-earn the opt-in on each
                        # run.
                        "memory_opt_in": False,
                        # Track 7 Follow-up (Task 3) — seed the replace-and-
                        # rehydrate compaction fields at reducer-safe defaults
                        # so every task graph starts from a known-good state.
                        # Reducer-annotated fields use the seed only on the
                        # FIRST write; subsequent node returns go through the
                        # reducer (max / any / replace). MUST be 0 / "" / False
                        # — NEVER None.
                        "summary": "",
                        "summarized_through_turn_index": 0,
                        "memory_flush_fired_this_task": False,
                        "last_super_step_message_count": 0,
                        "tier3_firings_count": 0,
                        "tier3_fatal_short_circuited": False,
                    }
                    initial_input = _payload
                else:
                    initial_input = None

                # Resume path: if this is a resumed task with a human response, use Command(resume=...)
                if not first_execution:
                    human_response = await self.pool.fetchval(
                        'SELECT human_response FROM tasks WHERE task_id = $1::uuid', task_id
                    )
                    if human_response:
                        payload = json.loads(human_response)
                        # Track 7 Task 13 — append hitl_resume to the
                        # user-facing conversation log.  One append per
                        # resume per checkpoint (idempotency-key scoped).
                        try:
                            convlog_repo = ConversationLogRepository(self.pool)
                            checkpoint_id_for_resume = (
                                await fetch_latest_checkpoint_id(self.pool, task_id)
                            )
                            payload_kind = payload.get("kind", "input")
                            if payload_kind == "approval":
                                resolution = (
                                    "approved"
                                    if bool(payload.get("approved"))
                                    else "rejected"
                                )
                            elif payload_kind == "follow_up":
                                resolution = "follow_up"
                            else:
                                resolution = "modified" if payload.get("message") else "cancelled"
                            user_note = payload.get("message") if isinstance(payload.get("message"), str) else None
                            origin_ref = f"hitl_resume:{checkpoint_id_for_resume or 'init'}"
                            resume_key = _convlog_key(
                                task_id=task_id,
                                checkpoint_id=checkpoint_id_for_resume,
                                origin_ref=origin_ref,
                            )
                            await convlog_repo.append_entry(
                                task_id=task_id,
                                tenant_id=tenant_id,
                                checkpoint_id=checkpoint_id_for_resume,
                                idempotency_key=resume_key,
                                kind="hitl_resume",
                                role="system",
                                content={
                                    "resolution": resolution,
                                    "user_note": user_note,
                                },
                                metadata={
                                    "resolved_by": "user",
                                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                        except Exception:
                            logger.warning(
                                "conversation_log hitl_resume append skipped",
                                exc_info=True,
                            )
                        # Decode the documented HITL resume payload
                        # {"kind":"follow_up","message":"..."} -> inject new HumanMessage
                        # {"kind":"input","message":"blue"} -> resume value is the message
                        # {"kind":"approval","approved":true} -> resume value is the payload itself
                        if payload.get("kind") == "follow_up":
                            # Follow-up: inject new HumanMessage into existing
                            # conversation. Reset the opt-in flag so follow-up
                            # runs must re-earn it (Task 12 per-run reset
                            # invariant). Track 7 Task 2: always include the
                            # field — memory-disabled tasks simply hold False.
                            follow_up_payload: dict[str, Any] = {
                                "messages": [
                                    HumanMessage(
                                        content=payload.get("message", "")
                                    )
                                ],
                                "memory_opt_in": False,
                            }
                            initial_input = follow_up_payload
                        elif payload.get("kind") == "input":
                            resume_value = payload.get("message", "")
                            initial_input = Command(resume=resume_value)
                        else:
                            resume_value = payload  # approval payload passed through
                            initial_input = Command(resume=resume_value)

                # Track model name for per-step cost calculation
                model_name = agent_config.get("model", "claude-3-5-sonnet-latest")
                # Track cumulative costs for Task 4 budget enforcement (added later)
                cumulative_task_cost = 0
                hourly_cost = 0

                # Executing super-steps via astream
                # durability="sync" ensures checkpoints are committed before astream
                # yields, so the cost-ledger SELECT always finds the correct checkpoint_id.
                async for event in compiled_graph.astream(initial_input, config=config, stream_mode="updates", durability="sync"):
                    # Step 6: Cancellation Awareness
                    if cancel_event.is_set():
                        logger.warning("Task %s cancelled or lease revoked during execution.", task_id)
                        return

                    # Refresh sandbox timeout to prevent expiry during long tasks
                    if sandbox is not None:
                        try:
                            sandbox_timeout = sandbox_config.get("timeout_seconds", 3600)
                            await asyncio.to_thread(sandbox.set_timeout, sandbox_timeout)
                        except Exception:
                            logger.debug("sandbox_timeout_refresh_failed", extra={"task_id": task_id})

                    # Phase 2 Track 5 budget carve-out: the ``memory_write``
                    # super-step is a platform-directed closure step and MUST
                    # NOT trip ``budget_max_per_task``. Its summarizer LLM
                    # cost is written by ``_commit_memory_and_complete_task``
                    # directly (outside this per-step loop), which is why
                    # there's no ``event["agent"]`` payload to gate on here
                    # — the node returns a ``Command`` updating
                    # ``pending_memory``, not a new ``AIMessage``. Hourly
                    # spend still accrues via the same commit path. This
                    # explicit check provides defense in depth in case the
                    # pause enforcement ever widens to fire on non-agent
                    # nodes.
                    # Track 7 Task 8: same carve-out for ``compaction.tier3``
                    # — the Tier 3 summarizer LLM cost is written directly to
                    # the cost ledger by ``summarize_slice`` (Task 7); it must
                    # NOT also trip the per-task budget pause here.  The
                    # "compaction.tier3" key is used as the operation tag in
                    # the cost-ledger row and appears as the event key in
                    # the astream update dict when the summarizer runs.
                    if MEMORY_WRITE_NODE_NAME in event:
                        continue
                    if "compaction.tier3" in event:
                        continue

                    # Per-checkpoint incremental cost tracking
                    if "agent" in event:
                        for ai_msg in event["agent"].get("messages", []):
                            if hasattr(ai_msg, 'response_metadata') and ai_msg.response_metadata:
                                try:
                                    # Merge usage_metadata from the message object into
                                    # response_metadata so _extract_tokens can find it
                                    # (Bedrock Converse puts tokens in usage_metadata on
                                    # the message, not inside response_metadata).
                                    resp_meta = dict(ai_msg.response_metadata)
                                    if hasattr(ai_msg, 'usage_metadata') and ai_msg.usage_metadata:
                                        resp_meta.setdefault("usage_metadata", ai_msg.usage_metadata)
                                    step_cost, execution_metadata = await self._calculate_step_cost(
                                        resp_meta, model_name
                                    )
                                    async with self.pool.acquire() as cost_conn:
                                        checkpoint_id = await fetch_latest_checkpoint_id(
                                            cost_conn, task_id
                                        )
                                        if checkpoint_id:
                                            if step_cost > 0:
                                                try:
                                                    async with cost_conn.transaction():
                                                        cumulative_task_cost, hourly_cost = await self._record_step_cost(
                                                            cost_conn, task_id, tenant_id, agent_id, checkpoint_id, step_cost,
                                                            execution_metadata=execution_metadata,
                                                            worker_id=worker_id,
                                                        )
                                                    logger.debug(
                                                        "Task %s step cost: %d microdollars (cumulative: %d, hourly: %d)",
                                                        task_id, step_cost, cumulative_task_cost, hourly_cost,
                                                    )
                                                except LeaseRevokedException:
                                                    raise
                                                except Exception:
                                                    logger.warning("Per-step cost recording failed for task %s", task_id, exc_info=True)
                                                    cumulative_task_cost = 0
                                            else:
                                                # Cost is zero (unknown model or rounding), but still persist token metadata
                                                try:
                                                    async with cost_conn.transaction():
                                                        lease_ok = await cost_conn.fetchval(
                                                            '''SELECT 1 FROM tasks
                                                               WHERE task_id = $1::uuid
                                                                 AND tenant_id = $2
                                                                 AND status = 'running'
                                                                 AND lease_owner = $3
                                                               FOR UPDATE''',
                                                            task_id, tenant_id, worker_id,
                                                        )
                                                        if lease_ok is None:
                                                            raise LeaseRevokedException(
                                                                f"Lease revoked before metadata write for task {task_id}"
                                                            )
                                                        await set_execution_metadata(
                                                            cost_conn,
                                                            checkpoint_id=checkpoint_id,
                                                            task_id=task_id,
                                                            execution_metadata=execution_metadata,
                                                        )
                                                    logger.debug(
                                                        "Task %s step cost: 0 microdollars (metadata persisted)",
                                                        task_id,
                                                    )
                                                except LeaseRevokedException:
                                                    raise
                                                except Exception:
                                                    logger.warning("Execution metadata write failed for task %s", task_id, exc_info=True)
                                                cumulative_task_cost = 0
                                            # Budget enforcement after checkpoint-cost write
                                            if cumulative_task_cost > 0:
                                                was_paused = await self._check_budget_and_pause(
                                                    cost_conn, task_data, cumulative_task_cost, worker_id
                                                )
                                                if was_paused:
                                                    # Close MCP sessions before releasing lease on budget pause
                                                    if session_manager is not None:
                                                        await session_manager.close("paused")
                                                        session_manager = None  # Prevent double-close in finally
                                                    # Record sandbox cost before pausing
                                                    if sandbox is not None and sandbox_start_time is not None:
                                                        elapsed = time.monotonic() - sandbox_start_time
                                                        pause_sandbox_cost = int(
                                                            elapsed * sandbox_config.get("vcpu", 2) * 50000 / 3600
                                                        )
                                                        if pause_sandbox_cost > 0:
                                                            try:
                                                                async with self.pool.acquire() as sc_conn:
                                                                    await insert_cost_row(
                                                                        sc_conn,
                                                                        tenant_id=tenant_id,
                                                                        agent_id=agent_id,
                                                                        task_id=task_id,
                                                                        checkpoint_id='sandbox',
                                                                        cost_microdollars=pause_sandbox_cost,
                                                                    )
                                                            except Exception:
                                                                logger.warning(
                                                                    "sandbox_cost_recording_failed_on_budget_pause",
                                                                    extra={"task_id": task_id},
                                                                    exc_info=True,
                                                                )
                                                    # Pause sandbox before releasing lease on budget pause
                                                    if sandbox is not None and provisioner is not None:
                                                        await provisioner.pause(sandbox)
                                                        sandbox = None  # Prevent double-destroy in finally
                                                    return  # Stop execution — task is now paused
                                except LeaseRevokedException:
                                    # Propagate to the outer astream handler so the evicted worker
                                    # stops all further model/tool work instead of silently eating
                                    # the lease check and continuing the loop.
                                    raise
                                except Exception:
                                    logger.warning("Per-step cost tracking failed for task %s", task_id, exc_info=True)

                if cancel_event.is_set():
                    return

                # Check for pending interrupts (e.g., request_human_input called interrupt())
                final_state = await compiled_graph.aget_state(config)
                if final_state.tasks:
                    for task_obj in final_state.tasks:
                        if hasattr(task_obj, 'interrupts') and task_obj.interrupts:
                            # Graph paused due to interrupt() — handle as HITL pause
                            interrupt_data = task_obj.interrupts[0].value if task_obj.interrupts else {}
                            # Extract the last AI message text as context for the prompt
                            messages = final_state.values.get("messages", [])
                            ai_context = ""
                            for msg in reversed(messages):
                                if getattr(msg, "type", None) == "ai" and msg.content:
                                    # AI content can be a string or a list of content blocks
                                    if isinstance(msg.content, str):
                                        ai_context = msg.content
                                    elif isinstance(msg.content, list):
                                        text_parts = [b["text"] for b in msg.content if isinstance(b, dict) and b.get("type") == "text"]
                                        ai_context = "\n".join(text_parts)
                                    break
                            # Capture the original tool prompt before enrichment
                            original_tool_prompt = interrupt_data.get("prompt", "") if isinstance(interrupt_data, dict) else str(interrupt_data)
                            if ai_context and isinstance(interrupt_data, dict):
                                # Prepend the AI's text content to the prompt for full context
                                tool_prompt = interrupt_data.get("prompt", "")
                                interrupt_data["prompt"] = f"{ai_context}\n\n{tool_prompt}" if tool_prompt else ai_context
                            await self._handle_interrupt_from_state(task_data, interrupt_data, worker_id, original_tool_prompt=original_tool_prompt)
                            # Close MCP sessions before releasing lease on HITL pause
                            if session_manager is not None:
                                await session_manager.close("paused")
                                session_manager = None  # Prevent double-close in finally
                            # Record sandbox cost before pausing
                            if sandbox is not None and sandbox_start_time is not None:
                                elapsed = time.monotonic() - sandbox_start_time
                                hitl_sandbox_cost = int(
                                    elapsed * sandbox_config.get("vcpu", 2) * 50000 / 3600
                                )
                                if hitl_sandbox_cost > 0:
                                    try:
                                        async with self.pool.acquire() as sc_conn:
                                            await insert_cost_row(
                                                sc_conn,
                                                tenant_id=tenant_id,
                                                agent_id=agent_id,
                                                task_id=task_id,
                                                checkpoint_id='sandbox',
                                                cost_microdollars=hitl_sandbox_cost,
                                            )
                                    except Exception:
                                        logger.warning(
                                            "sandbox_cost_recording_failed_on_hitl_pause",
                                            extra={"task_id": task_id},
                                            exc_info=True,
                                        )
                            # Pause sandbox before releasing lease on HITL pause
                            if sandbox is not None and provisioner is not None:
                                await provisioner.pause(sandbox)
                                sandbox = None  # Prevent double-destroy in finally
                            return

                # Execution Finished successfully. Compute final output.
                messages = final_state.values.get("messages", [])
                output_content = messages[-1].content if messages else ""

                # Per-checkpoint cost tracking replaces end-of-task aggregation.
                # Costs are now written incrementally in the streaming loop above.

                # Sandbox cleanup and cost tracking
                if sandbox is not None and sandbox_start_time is not None:
                    sandbox_duration_seconds = time.monotonic() - sandbox_start_time
                    sandbox_vcpu = sandbox_config.get("vcpu", 2)
                    # E2B cost: $0.05/hour per vCPU, per-second billing
                    sandbox_cost_microdollars = int(
                        sandbox_duration_seconds * sandbox_vcpu * 50000 / 3600
                    )

                    logger.info(
                        "sandbox_cost_calculated",
                        extra={
                            "task_id": task_id,
                            "sandbox_id": sandbox.sandbox_id,
                            "duration_seconds": round(sandbox_duration_seconds, 1),
                            "vcpu": sandbox_vcpu,
                            "cost_microdollars": sandbox_cost_microdollars,
                        },
                    )

                    # Add sandbox cost to the task's cost via the cost ledger
                    if sandbox_cost_microdollars > 0:
                        try:
                            async with self.pool.acquire() as cost_conn:
                                await insert_cost_row(
                                    cost_conn,
                                    tenant_id=tenant_id,
                                    agent_id=agent_id,
                                    task_id=task_id,
                                    checkpoint_id='sandbox',
                                    cost_microdollars=sandbox_cost_microdollars,
                                )
                                # Also roll sandbox cost into the last checkpoint so that
                                # total_cost_microdollars (summed from checkpoints by the API) includes it.
                                await add_cost_to_latest_terminal_checkpoint(
                                    cost_conn,
                                    task_id=task_id,
                                    delta_microdollars=sandbox_cost_microdollars,
                                )
                        except Exception:
                            logger.warning(
                                "sandbox_cost_recording_failed",
                                extra={"task_id": task_id},
                                exc_info=True,
                            )

                    # Pause sandbox (not destroy) so follow-ups can reconnect.
                    # The sandbox_id stays in DB. E2B auto-destroys after the
                    # configured timeout if no follow-up arrives.
                    try:
                        await provisioner.pause(sandbox)
                        logger.info(
                            "sandbox_paused_on_completion",
                            extra={"task_id": task_id, "sandbox_id": sandbox.sandbox_id},
                        )
                    except Exception:
                        logger.warning(
                            "sandbox_pause_on_completion_failed",
                            extra={"task_id": task_id},
                            exc_info=True,
                        )

                    sandbox = None  # Prevent double-action in finally

                # Step 5: Flush Langfuse traces before marking complete
                langfuse_status = "skipped"
                if per_task_langfuse_client is not None:
                    langfuse_status = await self._flush_langfuse_with_retry(per_task_langfuse_client, task_id)
                    per_task_langfuse_client = None  # Prevent double-flush in finally

                # Step 6: Completion Path
                output_data = {"result": output_content}
                if langfuse_endpoint_id:
                    output_data["langfuse_status"] = langfuse_status

                # Phase 2 Track 5 Task 12 — post-commit gate. The stack-
                # enabled branch splits again on whether the run earned a
                # memory write: ``auto_write=True`` (always mode) or the
                # in-state ``memory_opt_in`` flag set by ``save_memory``
                # (agent_decides mode). When neither is true we fall through
                # to the memory-disabled branch below and complete the task
                # without a memory row.
                opt_in = bool(final_state.values.get("memory_opt_in", False)) \
                    if isinstance(final_state.values, dict) else False
                if memory_decision.stack_enabled and (
                    memory_decision.auto_write or opt_in
                ):
                    # Co-commit the memory UPSERT + FIFO trim + lease-
                    # validated task completion in one transaction. Read
                    # ``pending_memory`` from the final state values — the
                    # ``memory_write`` node just set it on the terminal
                    # branch.
                    pending_memory = read_pending_memory_from_state_values(
                        final_state.values
                    )
                    try:
                        await self._commit_memory_and_complete_task(
                            task_id=task_id,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            pending_memory=pending_memory,
                            agent_config=agent_config,
                            output=output_data,
                            worker_id=worker_id,
                        )
                        logger.info(
                            "Task %s completed with memory (cost: %d microdollars, langfuse: %s, mode=%s, opt_in=%s).",
                            task_id, cumulative_task_cost, langfuse_status,
                            memory_mode, opt_in,
                        )
                    except LeaseRevokedException:
                        logger.warning(
                            "Task %s memory commit skipped: lease no longer owned by this worker.",
                            task_id,
                        )
                    return

                # Memory-disabled branch — unchanged from pre-Track-5.
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        updated = await conn.fetchval(
                            '''UPDATE tasks
                               SET status='completed',
                                   output=$1,
                                   last_error_code=NULL,
                                   last_error_message=NULL,
                                   human_response=NULL,
                                   version=version+1,
                                   lease_owner=NULL,
                                   lease_expiry=NULL
                               WHERE task_id=$2::uuid
                                 AND status='running'
                                 AND lease_owner=$3
                               RETURNING task_id''',
                            json.dumps(output_data),
                            task_id,
                            worker_id,
                        )
                        if updated is not None:
                            # Track 3: Decrement running_task_count on completion
                            await decrement_running_count(conn, tenant_id, agent_id)
                            await _insert_task_event(
                                conn, task_id, tenant_id, agent_id,
                                "task_completed", "running", "completed",
                                worker_id,
                            )
                if updated is None:
                    logger.warning("Task %s completion skipped: lease no longer owned by this worker.", task_id)
                else:
                    logger.info("Task %s completed successfully (cost: %d microdollars, langfuse: %s).", task_id, cumulative_task_cost, langfuse_status)

            # Step 2: Wrap execution in timeout
            await asyncio.wait_for(run_astream(), timeout=task_timeout_seconds)

        except asyncio.TimeoutError:
            await self._handle_dead_letter(
                task_id, tenant_id, agent_id,
                "task_timeout", "Execution exceeded task logic timeout",
                memory_enabled=memory_enabled_for_task,
                memory_mode=memory_mode,
                agent_config=agent_config,
                task_input=task_input,
                retry_count=task_data.get("retry_count", 0),
                checkpointer=checkpointer,
            )
        except GraphRecursionError:
            await self._handle_dead_letter(
                task_id, tenant_id, agent_id,
                "max_steps_exceeded",
                f"Execution exceeded max_steps ({max_steps})",
                memory_enabled=memory_enabled_for_task,
                memory_mode=memory_mode,
                agent_config=agent_config,
                task_input=task_input,
                retry_count=task_data.get("retry_count", 0),
                checkpointer=checkpointer,
            )
        except GraphInterrupt as gi:
            await self._handle_interrupt(task_data, gi, worker_id)
        except _ContextExceededIrrecoverableError as e:
            # Track 7 Task 8: compaction hard-floor — context window exceeded
            # irrecoverably after all tiers.  Dead-letter with the dedicated
            # reason code so operators can distinguish this from general errors.
            await self._handle_dead_letter(
                task_id, tenant_id, agent_id,
                DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE,
                str(e),
                error_code=DEAD_LETTER_REASON_CONTEXT_EXCEEDED_IRRECOVERABLE,
                memory_enabled=memory_enabled_for_task,
                memory_mode=memory_mode,
                agent_config=agent_config,
                task_input=task_input,
                retry_count=task_data.get("retry_count", 0),
                checkpointer=checkpointer,
            )
        except LeaseRevokedException:
            # Lease was explicitly stripped before a checkpoint write
            logger.warning("Task %s raised LeaseRevokedException, stopping gracefully.", task_id)
            pass
        except Exception as e:
            # Step 4: Failure classification
            if self._is_retryable_error(e):
                await self._handle_retryable_error(
                    task_data,
                    e,
                    memory_enabled=memory_enabled_for_task,
                    memory_mode=memory_mode,
                    agent_config=agent_config,
                    task_input=task_input,
                    checkpointer=checkpointer,
                )
            else:
                await self._handle_dead_letter(
                    task_id, tenant_id, agent_id,
                    "non_retryable_error", str(e),
                    error_code="fatal_error",
                    memory_enabled=memory_enabled_for_task,
                    memory_mode=memory_mode,
                    agent_config=agent_config,
                    task_input=task_input,
                    retry_count=task_data.get("retry_count", 0),
                    checkpointer=checkpointer,
                )
        finally:
            if per_task_langfuse_client is not None:
                try:
                    per_task_langfuse_client.flush()
                except Exception:
                    logger.warning("Langfuse flush failed for task %s in finally block", task_id, exc_info=True)
            if session_manager is not None:
                try:
                    await session_manager.close()
                except Exception:
                    logger.warning("MCP session close failed for task %s in finally block", task_id, exc_info=True)
            if sandbox is not None and provisioner is not None:
                try:
                    await provisioner.pause(sandbox)
                    logger.info("Sandbox paused for task %s in finally block", task_id)
                except Exception:
                    logger.warning("Sandbox pause failed for task %s in finally block", task_id, exc_info=True)

    def _build_runnable_config(
        self,
        *,
        task_id: str,
        tenant_id: str,
        agent_id: str,
        max_steps: int,
        langfuse_credentials: dict | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": task_id,
            },
            "recursion_limit": max_steps,
        }

        if langfuse_credentials is None:
            return config

        try:
            callback = self._build_langfuse_callback(
                public_key=langfuse_credentials["public_key"],
            )
            config["callbacks"] = [callback]
            config["metadata"] = {
                "langfuse_session_id": task_id,
                "langfuse_user_id": tenant_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "tenant_id": tenant_id,
            }
        except Exception:
            logger.warning("Failed to build Langfuse callback for task %s, continuing without traces", task_id, exc_info=True)

        return config

    async def _flush_langfuse_with_retry(self, client: Langfuse, task_id: str, max_retries: int = 3) -> str:
        """Flush Langfuse client with retries. Returns 'sent' or 'failed'."""
        for attempt in range(1, max_retries + 1):
            try:
                client.flush()
                return "sent"
            except Exception:
                if attempt < max_retries:
                    logger.warning(
                        "Langfuse flush attempt %d/%d failed for task %s, retrying...",
                        attempt, max_retries, task_id, exc_info=True,
                    )
                    await asyncio.sleep(attempt)  # Simple linear backoff: 1s, 2s
                else:
                    logger.warning(
                        "Langfuse flush failed after %d attempts for task %s",
                        max_retries, task_id, exc_info=True,
                    )
        return "failed"

    def _build_langfuse_callback(self, *, public_key: str) -> CallbackHandler:
        # Task metadata (task_id, agent_id, tenant_id) is propagated via LangChain
        # config["metadata"] and automatically attached to the Langfuse trace.
        return CallbackHandler(public_key=public_key)

    async def _await_or_cancel(
        self,
        awaitable: Awaitable[Any],
        cancel_event: asyncio.Event,
        *,
        task_id: str,
        operation: str,
    ) -> Any:
        if cancel_event.is_set():
            raise LeaseRevokedException(
                f"Task {task_id} cancelled or lease revoked before {operation} started."
            )

        operation_task = asyncio.create_task(awaitable)
        cancel_task = asyncio.create_task(cancel_event.wait())

        try:
            done, _ = await asyncio.wait(
                {operation_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancel_task in done and cancel_event.is_set():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                raise LeaseRevokedException(
                    f"Task {task_id} cancelled or lease revoked during {operation}."
                )

            return await operation_task
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    @staticmethod
    def _walk_exception_chain(e: Exception):
        """Yield each exception in the __cause__/__context__ chain (including e itself)."""
        current = e
        for _ in range(5):
            if current is None:
                break
            yield current
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    def _extract_status_code(self, e: Exception) -> int | None:
        """Walk the exception chain to find an HTTP status code.
        Works with both anthropic.APIStatusError and openai.APIStatusError."""
        for exc in self._walk_exception_chain(e):
            code = getattr(exc, "status_code", None)
            if isinstance(code, int):
                return code
        return None

    def _get_retry_after(self, e: Exception) -> float | None:
        """Extract retry-after seconds from the error's HTTP response headers."""
        for exc in self._walk_exception_chain(e):
            resp = getattr(exc, "response", None)
            if resp is not None:
                retry_after = getattr(resp, "headers", {}).get("retry-after")
                if retry_after:
                    try:
                        return float(retry_after)
                    except (ValueError, TypeError):
                        pass
                return None
        return None

    # Status codes that are safe to retry (transient server / rate-limit errors)
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}

    def _is_rate_limit_error(self, e: Exception) -> bool:
        """Check if the exception is a rate limit error (429)."""
        status = self._extract_status_code(e)
        if status == 429:
            return True
        # Fallback: string heuristics for wrapped/unknown providers
        error_str = str(e).lower()
        if "429" in error_str or "rate limit" in error_str or "rate exceeded" in error_str:
            return True
        return False

    def _is_retryable_error(self, e: Exception) -> bool:
        """Determines if the exception should trigger a retry or immediate dead letter."""
        # Check exception type first (most reliable signal)
        if isinstance(e, (ToolTransportError, McpToolCallError)):
            return True
        if isinstance(e, (ConnectionError, TimeoutError)):
            return True
        # botocore timeouts: botocore.exceptions.ReadTimeoutError /
        # ConnectTimeoutError do NOT inherit from Python's builtin
        # TimeoutError (urllib3 defines its own same-named base). Import
        # lazily to avoid coupling the generic classifier to a specific
        # provider SDK at module-load time.
        try:
            from botocore.exceptions import ReadTimeoutError as _BotoReadTimeoutError
            from botocore.exceptions import ConnectTimeoutError as _BotoConnectTimeoutError

            if isinstance(e, (_BotoReadTimeoutError, _BotoConnectTimeoutError)):
                return True
        except ImportError:
            pass

        # Use HTTP status code from the provider exception if available
        status = self._extract_status_code(e)
        if status is not None:
            return status in self._RETRYABLE_STATUS_CODES

        # Fallback: string heuristics for errors without a status code
        error_str = str(e).lower()

        if "429" in error_str or "rate limit" in error_str or "rate exceeded" in error_str:
            return True
        if re.search(r'\b50[0234]\b', error_str):
            return True
        # Network-timeout phrasing produced by botocore / httpx / urllib3
        # when no HTTP status was received. Matches the exact prefixes
        # "Read timeout" and "Connect timeout" to avoid overmatching
        # unrelated error strings that happen to contain the word "timeout".
        if "read timeout" in error_str or "connect timeout" in error_str:
            return True
        if "validation" in error_str or "invalid" in error_str or "unsupported" in error_str or "pydantic" in error_str:
            return False
        if re.search(r'\b40[0-4]\b', error_str):
            return False

        # Default unknown exceptions to non-retryable
        return False

    async def _check_budget_and_pause(
        self,
        conn,
        task_data: dict,
        cumulative_task_cost: int,
        worker_id: str,
    ) -> bool:
        """Check budget limits after a checkpoint-cost write. Returns True if task was paused."""
        tenant_id = task_data["tenant_id"]
        agent_id = task_data["agent_id"]

        # Re-read agent budget settings (may have changed since task started)
        agent = await conn.fetchrow(
            '''SELECT budget_max_per_task, budget_max_per_hour
               FROM agents WHERE tenant_id = $1 AND agent_id = $2''',
            tenant_id, agent_id
        )
        if not agent:
            return False

        budget_max_per_task = agent['budget_max_per_task']
        budget_max_per_hour = agent['budget_max_per_hour']

        # Check per-task budget (takes precedence if both exceeded)
        per_task_exceeded = cumulative_task_cost > budget_max_per_task

        # Check hourly budget (rolling 60-minute window from canonical ledger)
        hour_cost = await sum_hourly_cost_for_agent(conn, tenant_id, agent_id)
        hourly_exceeded = hour_cost > budget_max_per_hour

        if not per_task_exceeded and not hourly_exceeded:
            return False

        # Determine pause reason (per-task takes precedence)
        if per_task_exceeded:
            pause_reason = 'budget_per_task'
            pause_details = {
                'budget_max_per_task': budget_max_per_task,
                'observed_task_cost_microdollars': cumulative_task_cost,
                'recovery_mode': 'manual_resume_after_budget_increase',
            }
            resume_eligible_at = None
        else:
            pause_reason = 'budget_per_hour'
            pause_details = {
                'budget_max_per_hour': budget_max_per_hour,
                'observed_hour_cost_microdollars': hour_cost,
                'recovery_mode': 'automatic_after_window_clears',
            }
            # Estimate when enough spend ages out: find the oldest ledger entry
            # in the window and add 60 minutes
            oldest_entry_time = await min_created_at_in_hour_window(
                conn, tenant_id, agent_id
            )
            if oldest_entry_time:
                resume_eligible_at = oldest_entry_time + timedelta(minutes=60)
            else:
                resume_eligible_at = None

        await self._execute_budget_pause(
            conn, task_data, worker_id, pause_reason, pause_details, resume_eligible_at
        )
        return True

    async def _execute_budget_pause(
        self,
        conn,
        task_data: dict,
        worker_id: str,
        pause_reason: str,
        pause_details: dict,
        resume_eligible_at: datetime | None,
    ):
        """Transition a running task to paused for budget exhaustion."""
        task_id = str(task_data["task_id"])
        tenant_id = task_data["tenant_id"]
        agent_id = task_data["agent_id"]

        # Atomically: update task, decrement running_task_count, record event
        async with conn.transaction():
            # 1. Transition task to paused (lease-validated)
            result = await conn.fetchrow(
                '''UPDATE tasks
                   SET status = 'paused',
                       pause_reason = $1,
                       pause_details = $2::jsonb,
                       resume_eligible_at = $3,
                       lease_owner = NULL,
                       lease_expiry = NULL,
                       human_response = NULL,
                       version = version + 1,
                       updated_at = NOW()
                   WHERE task_id = $4::uuid
                     AND lease_owner = $5
                   RETURNING task_id''',
                pause_reason,
                json.dumps(pause_details),
                resume_eligible_at,
                task_id,
                worker_id,
            )

            if not result:
                logger.warning("Budget pause failed for task %s: lease no longer owned", task_id)
                return

            # 2. Decrement running_task_count (use upsert for robustness)
            await decrement_running_count(conn, tenant_id, agent_id)

            # 3. Record task_paused event
            # NOTE: _insert_task_event is a MODULE-LEVEL function, not a method
            event_details = {
                'pause_reason': pause_reason,
                **pause_details,
            }
            if resume_eligible_at:
                event_details['resume_eligible_at'] = resume_eligible_at.isoformat()
            await _insert_task_event(
                conn, task_id, tenant_id, agent_id,
                event_type='task_paused',
                status_before='running',
                status_after='paused',
                worker_id=worker_id,
                details=event_details,
            )

        logger.info(
            "Task %s paused: %s (cost: %s)",
            task_id, pause_reason, pause_details,
        )

    async def _handle_interrupt_from_state(self, task_data: dict, interrupt_data: dict, worker_id: str, *, original_tool_prompt: str | None = None):
        """Handle an interrupt detected via graph state inspection."""
        if not isinstance(interrupt_data, dict):
            interrupt_data = {"type": "input", "prompt": str(interrupt_data)}
        if original_tool_prompt is None:
            original_tool_prompt = interrupt_data.get("prompt", "")
        await self._handle_interrupt_internal(task_data, interrupt_data, worker_id, original_tool_prompt=original_tool_prompt)

    async def _handle_interrupt(self, task_data: dict, interrupt_exc: GraphInterrupt, worker_id: str):
        """Handle a GraphInterrupt exception by transitioning the task to a waiting state."""
        interrupt_values = interrupt_exc.args[0] if interrupt_exc.args else [{}]
        interrupt_data = interrupt_values[0] if isinstance(interrupt_values, list) and interrupt_values else {}
        if not isinstance(interrupt_data, dict):
            interrupt_data = {"type": "input", "prompt": str(interrupt_data)}
        await self._handle_interrupt_internal(task_data, interrupt_data, worker_id)

    async def _handle_interrupt_internal(self, task_data: dict, interrupt_data: dict, worker_id: str, *, original_tool_prompt: str | None = None):
        """Core interrupt handling: transition task to waiting state, release lease, record event."""
        task_id = str(task_data["task_id"])
        tenant_id = task_data["tenant_id"]
        agent_id = task_data.get("agent_id") or "unknown"

        interrupt_type = interrupt_data.get("type", "input")

        if interrupt_type == "approval":
            new_status = "waiting_for_approval"
            event_type = "task_approval_requested"
        else:
            new_status = "waiting_for_input"
            event_type = "task_input_requested"

        # Calculate timeout (24 hours from now)
        timeout_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # Atomically: update task to waiting state + release lease + insert event
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if interrupt_type == "approval":
                    updated = await conn.fetchval('''
                        UPDATE tasks SET status = $1,
                            pending_approval_action = $2::jsonb,
                            human_input_timeout_at = $3,
                            lease_owner = NULL, lease_expiry = NULL,
                            version = version + 1, updated_at = NOW()
                        WHERE task_id = $4::uuid AND lease_owner = $5
                        RETURNING task_id
                    ''', new_status, json.dumps(interrupt_data.get("action", {})),
                        timeout_at, task_id, worker_id)
                else:
                    updated = await conn.fetchval('''
                        UPDATE tasks SET status = $1,
                            pending_input_prompt = $2,
                            human_input_timeout_at = $3,
                            lease_owner = NULL, lease_expiry = NULL,
                            version = version + 1, updated_at = NOW()
                        WHERE task_id = $4::uuid AND lease_owner = $5
                        RETURNING task_id
                    ''', new_status, interrupt_data.get("prompt", "Agent is requesting input"),
                        timeout_at, task_id, worker_id)

                if updated is not None:
                    # Track 3: Decrement running_task_count on HITL pause
                    await decrement_running_count(conn, tenant_id, agent_id)
                    # Insert event in same transaction only if the UPDATE affected a row
                    event_details = None
                    if interrupt_type == "input":
                        # Use original tool argument, not the AI-context-enriched prompt
                        event_details = {"prompt": original_tool_prompt if original_tool_prompt is not None else interrupt_data.get("prompt", "")}
                    elif interrupt_type == "approval":
                        event_details = {"action": interrupt_data.get("action", {})}
                    await _insert_task_event(
                        conn, task_id, tenant_id, agent_id, event_type,
                        "running", new_status, worker_id=worker_id,
                        details=event_details,
                    )

        if updated is None:
            logger.warning("Task %s interrupt handling skipped: lease no longer owned by this worker.", task_id)
        else:
            logger.info("Task %s paused: %s (timeout: %s)", task_id, new_status, timeout_at)
            # Track 7 Task 13 — append hitl_pause to the user-facing
            # conversation log.  Idempotency key is scoped to the
            # checkpoint id (pause per checkpoint fires exactly once).
            try:
                convlog_repo = ConversationLogRepository(self.pool)
                checkpoint_id = await fetch_latest_checkpoint_id(self.pool, task_id)
                prompt_for_log = (
                    original_tool_prompt
                    if original_tool_prompt is not None
                    else interrupt_data.get("prompt", "") or ""
                )
                reason_for_log = (
                    "tool_requires_approval"
                    if interrupt_type == "approval"
                    else interrupt_data.get("reason", "agent_requested")
                )
                tool_name_for_log = None
                if isinstance(interrupt_data, dict):
                    tool_name_for_log = (
                        interrupt_data.get("tool_name")
                        or (interrupt_data.get("action") or {}).get("tool_name")
                    )
                origin_ref = f"hitl_pause:{checkpoint_id or 'init'}"
                key = _convlog_key(
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    origin_ref=origin_ref,
                )
                await convlog_repo.append_entry(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    checkpoint_id=checkpoint_id,
                    idempotency_key=key,
                    kind="hitl_pause",
                    role="system",
                    content={
                        "reason": reason_for_log,
                        "prompt_to_user": prompt_for_log if prompt_for_log else None,
                    },
                    metadata={
                        "checkpoint_id": checkpoint_id or "",
                        "tool_name": tool_name_for_log,
                    },
                )
            except Exception:
                # Best-effort — never fail the pause path.
                logger.warning(
                    "conversation_log hitl_pause append skipped",
                    exc_info=True,
                )

    async def _handle_retryable_error(
        self,
        task_data: dict[str, Any],
        e: Exception,
        *,
        memory_enabled: bool = False,
        memory_mode: str = "always",
        agent_config: dict[str, Any] | None = None,
        task_input: str | None = None,
        checkpointer: PostgresDurableCheckpointer | None = None,
    ):
        task_id = str(task_data["task_id"])
        tenant_id = task_data.get("tenant_id", "default")
        agent_id = task_data.get("agent_id") or "unknown"
        retry_count = task_data.get("retry_count", 0)
        max_retries = task_data.get("max_retries", 3)
        worker_pool_id = self.config.worker_pool_id

        if retry_count >= max_retries:
            await self._handle_dead_letter(
                task_id, tenant_id, agent_id,
                "retries_exhausted",
                f"Max retries reached. Last error: {e}",
                memory_enabled=memory_enabled,
                memory_mode=memory_mode,
                agent_config=agent_config,
                task_input=task_input,
                retry_count=retry_count,
                checkpointer=checkpointer,
            )
            return

        new_retry_count = retry_count + 1
        backoff_seconds = min(300, 2 ** new_retry_count)
        retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)

        worker_id = self.config.worker_id
        error_msg = str(e)[:1024]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    '''UPDATE tasks
                       SET status='queued',
                           retry_count=$1,
                           retry_after=$2,
                           retry_history=COALESCE(retry_history, '[]'::jsonb) || jsonb_build_array(NOW()),
                           last_error_code='retryable_error',
                           last_error_message=$3,
                           version=version+1,
                           lease_owner=NULL,
                           lease_expiry=NULL
                       WHERE task_id=$4::uuid
                         AND status='running'
                         AND lease_owner=$5
                       RETURNING task_id''',
                    new_retry_count,
                    retry_after,
                    error_msg,
                    task_id,
                    worker_id,
                )
                if updated is None:
                    logger.warning("Task %s retry-requeue skipped: lease no longer owned by this worker.", task_id)
                    return
                # Track 3: Decrement running_task_count on retry requeue
                await decrement_running_count(conn, tenant_id, agent_id)
                await _insert_task_event(
                    conn, task_id, tenant_id, agent_id,
                    "task_retry_scheduled", "running", "queued",
                    worker_id, error_code="retryable_error",
                    error_message=error_msg,
                    details={"retry_count": new_retry_count, "retry_after": str(retry_after)},
                )
                # Re-queue notification
                await conn.execute("SELECT pg_notify('new_task', $1)", worker_pool_id)

        logger.info("Task %s hit retryable error. Requeued (try %d).", task_id, new_retry_count)

    async def _handle_dead_letter(
        self,
        task_id: str,
        tenant_id: str,
        agent_id: str,
        reason: str,
        error_msg: str,
        error_code: str | None = None,
        *,
        memory_enabled: bool = False,
        memory_mode: str = "always",
        agent_config: dict[str, Any] | None = None,
        task_input: str | None = None,
        retry_count: int | None = None,
        checkpointer: PostgresDurableCheckpointer | None = None,
    ):
        """Transition a task to ``dead_letter`` with lease validation.

        Phase 2 Track 5 Task 8 adds an optional memory-write branch **inside
        the same transaction** as the task UPDATE. Gating rules (all must
        hold to write a row):

        * ``memory_enabled`` is True (``decision.stack_enabled`` — agent.
          memory.enabled AND ``tasks.memory_mode ∈ {always, agent_decides}``
          — caller's pre-computed decision).
        * ``reason != 'cancelled_by_user'`` — cancellation writes nothing.
        * At least one observation recorded in the most recent checkpoint.
        * Task 12 additional gate for ``memory_mode='agent_decides'`` — the
          agent must have called ``save_memory`` (``memory_opt_in=True`` in
          the latest checkpoint) before the failure. Without opt-in the
          dead-letter template is suppressed so the customer's
          "only-remember-when-worth-remembering" intent survives crashes.

        The row is template-only (``summarizer_model_id='template:dead_letter'``,
        ``outcome='failed'``) — no LLM call. Embedding is still attempted when
        a ``pool`` / embedding client is available; on provider failure the
        row is written with ``content_vec=NULL``.

        On lease loss at step (5) below, the whole transaction rolls back —
        no orphan memory row.
        """
        worker_id = self.config.worker_id
        error_msg = str(error_msg)[:1024]
        effective_error_code = error_code or reason

        # Phase 2 Track 5 Task 8 — memory hook pre-work (outside tx).
        #
        # We resolve the observations + template pending_memory BEFORE opening
        # the transaction so the transaction body stays tight around the DB
        # writes. The embedding call (if any) also happens here because it is
        # a network call that we don't want wrapped inside a DB transaction.
        pending_memory: dict[str, Any] | None = None
        memory_write_attempted = False
        if (
            memory_enabled
            and reason != DEAD_LETTER_REASON_CANCELLED_BY_USER
            and checkpointer is not None
        ):
            observations = await self._read_observations_from_checkpoint(
                checkpointer, task_id
            )
            # Phase 2 Track 5 Task 12 — ``agent_decides`` mode additionally
            # requires that the agent opted in (via ``save_memory``) before
            # the failure. Read the last-checkpointed value of
            # ``memory_opt_in`` so a crash that occurred BEFORE the opt-in
            # suppresses the dead-letter template, preserving the customer's
            # "only remember when worth remembering" intent.
            opt_in_required = memory_mode == "agent_decides"
            opt_in_confirmed = False
            if opt_in_required:
                opt_in_confirmed = (
                    await self._read_memory_opt_in_from_checkpoint(
                        checkpointer, task_id
                    )
                )
            if observations and (not opt_in_required or opt_in_confirmed):
                memory_write_attempted = True
                pending_memory = build_pending_memory_dead_letter_template(
                    task_input=task_input,
                    observations=observations,
                    retry_count=retry_count,
                    last_error_code=effective_error_code,
                    last_error_message=error_msg,
                )
                # Best-effort embedding. A failure here must NOT sink the
                # memory row — invariant: observations-bearing genuine
                # failures always produce a row.
                try:
                    embed_result = await _default_compute_embedding(
                        _build_dead_letter_embedding_text(pending_memory),
                        pool=self.pool,
                    )
                except Exception:
                    logger.warning(
                        "memory.deadletter.embedding_unexpected_exception "
                        "task_id=%s",
                        task_id,
                        exc_info=True,
                    )
                    embed_result = None
                if embed_result is None:
                    pending_memory["content_vec"] = None
                    pending_memory["embedding_tokens"] = 0
                    pending_memory["embedding_cost_microdollars"] = 0
                else:
                    pending_memory["content_vec"] = list(embed_result.vector)
                    pending_memory["embedding_tokens"] = embed_result.tokens
                    pending_memory["embedding_cost_microdollars"] = (
                        embed_result.cost_microdollars
                    )

        memory_id: Any = None
        trim_evicted = 0
        memory_written = False
        lease_lost = False

        class _DeadLetterLeaseLost(Exception):
            """Internal sentinel used to roll back the dead-letter tx
            atomically when the lease-validated UPDATE returns no row."""

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Memory UPSERT (if any) runs FIRST inside the tx so
                    # a lease mismatch on the task UPDATE below rolls back
                    # the memory row atomically — no orphan row survives.
                    if pending_memory is not None:
                        max_entries = max_entries_for_agent(agent_config)
                        entry = {
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "task_id": task_id,
                            "title": pending_memory["title"],
                            "summary": pending_memory["summary"],
                            "observations": list(
                                pending_memory.get("observations_snapshot") or []
                            ),
                            "outcome": pending_memory.get("outcome", "failed"),
                            "tags": list(pending_memory.get("tags") or []),
                            "content_vec": pending_memory.get("content_vec"),
                            "summarizer_model_id": pending_memory.get(
                                "summarizer_model_id"
                            ),
                        }
                        upserted = await upsert_memory_entry(conn, entry)
                        memory_id = upserted["memory_id"]
                        inserted_branch = upserted["inserted"]
                        memory_written = True

                        if inserted_branch:
                            post_insert_count = await count_entries_for_agent(
                                conn, tenant_id, agent_id
                            )
                            if post_insert_count > max_entries:
                                trim_evicted = await trim_oldest(
                                    conn,
                                    tenant_id=tenant_id,
                                    agent_id=agent_id,
                                    max_entries=max_entries,
                                    keep_memory_id=memory_id,
                                )

                        # Embedding cost ledger (template write has zero
                        # summarizer cost — no LLM call). Only recorded when
                        # an embedding was actually computed.
                        if pending_memory.get("content_vec") is not None:
                            checkpoint_id = await fetch_latest_terminal_checkpoint_id(
                                conn, task_id
                            )
                            if checkpoint_id:
                                embedding_cost = int(
                                    pending_memory.get(
                                        "embedding_cost_microdollars"
                                    )
                                    or 0
                                )
                                await insert_cost_row(
                                    conn,
                                    tenant_id=tenant_id,
                                    agent_id=agent_id,
                                    task_id=task_id,
                                    checkpoint_id=checkpoint_id,
                                    cost_microdollars=embedding_cost,
                                )

                    # 2. Lease-validated task dead-letter update.
                    #
                    # Clear ``human_response`` alongside the status flip so a
                    # subsequent redrive does NOT re-inject the pending
                    # follow-up / input / approval payload. The message is
                    # already in ``state["messages"]`` via the pre-crash
                    # checkpoint (durability="sync"), so redrive resumes
                    # with the message present; re-reading human_response
                    # would duplicate it (observed on task 75f5a223 —
                    # second follow-up appeared twice in the journal after
                    # redrive, rendering twice in the Console).
                    updated = await conn.fetchval(
                        '''UPDATE tasks
                           SET status='dead_letter',
                               dead_letter_reason=$1,
                               last_error_message=$2,
                               last_error_code=$3,
                               last_worker_id=$4,
                               dead_lettered_at=NOW(),
                               human_response=NULL,
                               version=version+1,
                               lease_owner=NULL,
                               lease_expiry=NULL
                           WHERE task_id=$5::uuid
                             AND status='running'
                             AND lease_owner=$6
                           RETURNING task_id''',
                        reason,
                        error_msg,
                        effective_error_code,
                        worker_id,
                        task_id,
                        worker_id,
                    )
                    if updated is None:
                        # Raise an internal sentinel to roll back the entire
                        # transaction (memory UPSERT + task UPDATE). Caught
                        # at the outer scope so we preserve the pre-Task-8
                        # "log-and-return" semantics that callers depend on.
                        raise _DeadLetterLeaseLost(
                            f"Lease revoked before dead-letter write for task {task_id}"
                        )

                    # Track 3: Decrement running_task_count on dead-letter
                    await decrement_running_count(conn, tenant_id, agent_id)
                    await _insert_task_event(
                        conn, task_id, tenant_id, agent_id,
                        "task_dead_lettered", "running", "dead_letter",
                        worker_id, error_code=effective_error_code,
                        error_message=error_msg,
                        details={"dead_letter_reason": reason},
                    )
        except _DeadLetterLeaseLost:
            lease_lost = True
            memory_written = False  # Rolled back with the tx.
            memory_id = None
            trim_evicted = 0
            logger.warning(
                "Task %s dead-letter skipped: lease no longer owned by this worker.",
                task_id,
            )
            return

        # Emit the dead-letter memory structured log AFTER the commit so the
        # "committed" claim is truthful.
        if memory_written:
            logger.info(
                "memory.deadletter.template tenant_id=%s agent_id=%s "
                "task_id=%s reason=%s observation_count=%d memory_id=%s "
                "trim_evicted=%d content_vec_null=%s",
                tenant_id, agent_id, task_id, reason,
                len(pending_memory.get("observations_snapshot") or [])
                if pending_memory else 0,
                memory_id, trim_evicted,
                pending_memory.get("content_vec") is None if pending_memory else True,
            )
        elif memory_write_attempted:
            # This branch is unreachable today (we only set the flag when we
            # also populate pending_memory), but we keep it so a future
            # divergence is noticed in logs rather than silently drops.
            logger.warning(
                "memory.deadletter.skipped_after_attempt task_id=%s", task_id
            )

        logger.error("Task %s dead-lettered: %s (msg: %s)", task_id, reason, error_msg)

    async def _read_observations_from_checkpoint(
        self,
        checkpointer: PostgresDurableCheckpointer,
        task_id: str,
    ) -> list[str]:
        """Read ``observations`` out of the latest checkpoint's state.

        Returns ``[]`` on any read failure so the dead-letter hook gracefully
        treats a missing/corrupt checkpoint as "no observations, skip memory
        write" rather than blocking the dead-letter transition.
        """
        try:
            config: dict[str, Any] = {"configurable": {"thread_id": task_id}}
            tup = await checkpointer.aget_tuple(config)
            if tup is None:
                return []
            checkpoint = getattr(tup, "checkpoint", None) or {}
            if not isinstance(checkpoint, dict):
                return []
            values = checkpoint.get("channel_values")
            if not isinstance(values, dict):
                return []
            obs = values.get("observations") or []
            if isinstance(obs, list):
                return [str(x) for x in obs if x is not None]
            return []
        except Exception:
            logger.warning(
                "memory.deadletter.observations_read_failed task_id=%s",
                task_id,
                exc_info=True,
            )
            return []

    async def _read_memory_opt_in_from_checkpoint(
        self,
        checkpointer: PostgresDurableCheckpointer,
        task_id: str,
    ) -> bool:
        """Read ``memory_opt_in`` from the latest checkpoint's state.

        Task 12 — used by the dead-letter hook to decide whether an
        ``agent_decides`` run had opted in before the failure. Missing /
        corrupt / absent → ``False`` (degrade to "not opted in") so a
        checkpoint read failure never silently upgrades a dead-letter into
        a memory write.
        """
        try:
            config: dict[str, Any] = {"configurable": {"thread_id": task_id}}
            tup = await checkpointer.aget_tuple(config)
            if tup is None:
                return False
            checkpoint = getattr(tup, "checkpoint", None) or {}
            if not isinstance(checkpoint, dict):
                return False
            values = checkpoint.get("channel_values")
            if not isinstance(values, dict):
                return False
            return bool(values.get("memory_opt_in", False))
        except Exception:
            logger.warning(
                "memory.deadletter.opt_in_read_failed task_id=%s",
                task_id,
                exc_info=True,
            )
            return False


def _build_dead_letter_embedding_text(pending_memory: dict[str, Any]) -> str:
    """Concatenate the text that seeds the dead-letter ``content_vec``.

    Matches the generated ``content_tsv`` expression in migration 0011 so
    search-time BM25 and embedding-time vector share a single content
    surface. Mirrors :func:`executor.memory_graph._build_embedding_text`
    but lives at module scope because the dead-letter hook uses a template
    dict rather than running through the ``memory_write_node`` path.
    """
    parts = [
        pending_memory.get("title") or "",
        pending_memory.get("summary") or "",
        " ".join(pending_memory.get("observations_snapshot") or []),
        " ".join(pending_memory.get("tags") or []),
    ]
    return " ".join(p for p in parts if p).strip()


async def _insert_task_event(
    conn,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    event_type: str,
    status_before: str | None,
    status_after: str | None,
    worker_id: str | None,
    error_code: str | None = None,
    error_message: str | None = None,
    details: dict | None = None,
):
    """Insert a task event on the current transaction-scoped connection.

    Must be called inside an active transaction so the event INSERT commits
    or rolls back atomically with the paired task-state mutation.
    """
    await conn.execute(
        '''INSERT INTO task_events (tenant_id, task_id, agent_id, event_type,
                                    status_before, status_after, worker_id,
                                    error_code, error_message, details)
           VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)''',
        tenant_id, task_id, agent_id, event_type,
        status_before, status_after, worker_id,
        error_code, error_message, json.dumps(details or {}),
    )
