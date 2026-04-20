"""Phase 2 Track 7 Follow-up Task 5 — agent-facing ``recall_tool_result`` tool.

Retrieves a previously offloaded tool RESULT or tool-call ARG from the Tier 0
artifact store (Task 4). Registered automatically per-task when the agent
config's ``context_management.offload_tool_results`` flag is ``true``
(default). The tool's URI is *reconstructed* from the closure-bound
``(tenant_id, task_id)`` plus the agent-supplied ``tool_call_id`` — the LLM
cannot broaden scope or point at a different tenant / task.

Design constraints (see task-5 spec §Contract):

1. Two shapes folded into one tool:
     * ``recall_tool_result(tool_call_id)`` — retrieve a tool RESULT.
     * ``recall_tool_result(tool_call_id, arg_key="content")`` — retrieve a
       tool-call ARG. The agent reads ``arg_key`` out of the
       ``[tool arg 'content' ... @ toolresult://...]`` placeholder.
2. Content-hash lookup: Task 4's URI scheme includes ``{content_hash}.txt``.
   The agent supplies the ``tool_call_id`` only; the tool lists the S3
   prefix ``tool-results/{tenant_id}/{task_id}/{tool_call_id}[/args/{k}]/``
   and uses the single matching hash. If the prefix holds multiple hashes
   (provider retry produced multiple offloads), returns an "ambiguous"
   error telling the agent to retry with a fresher placeholder.
3. Error-class differentiation (ALL return a str — never raise):
     * Malformed URI (should be impossible once we reconstruct) →
       "Error: not a valid tool result id".
     * Cross-task / cross-tenant URI →
       "Error: tool_call_id belongs to a different task or tenant".
     * ``store.get`` returns ``None`` (NoSuchKey) →
       "Error: content not available (artifact may have been purged)".
     * ``store.get`` raises → "Error: artifact store temporarily
       unavailable; retry or continue without this content".
4. Special ingestion rule (enforced in ``executor.graph.agent_node`` via the
   ingestion bypass hook at tool-node boundaries): the ``ToolMessage``
   returned here BYPASSES Task 4's ingestion offload and carries
   ``additional_kwargs={"recalled": True, "original_tool_call_id": ...}``.
   This tool is only responsible for the return string; the graph wires the
   bypass.

The tool has NO LLM cost. It makes at most one ``list`` + one ``get`` call
against the artifact store per invocation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Iterable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from executor.compaction.tool_result_store import (
    ToolResultArtifactStore,
    ToolResultURI,
    parse_tool_result_uri,
)

logger = logging.getLogger(__name__)


RECALL_TOOL_RESULT_NAME: str = "recall_tool_result"

RECALL_TOOL_RESULT_DESCRIPTION: str = (
    "Retrieve the full content of a previously offloaded tool output or "
    "tool-call argument. Takes the tool_call_id of that earlier call. "
    "Older tool results / args may appear in your context as placeholders "
    "like `[tool result 47823 bytes @ toolresult://... preview: ...]` or "
    "`[tool arg 'content' 47823 bytes @ toolresult://... preview: ...]`. "
    "Call this tool to see the full content. For a tool RESULT pass only "
    "tool_call_id; for a tool-call ARG also pass arg_key (the quoted key in "
    "the placeholder). Returns the original content as a string. Fetched "
    "content counts toward this turn's context budget."
)


RECALL_TOOL_RESULT_SYSTEM_PROMPT_HINT: str = (
    "Older tool outputs and large string arguments in this task may have "
    "been offloaded and now appear as placeholders of the form "
    "`[tool result N bytes @ toolresult://... preview: ...]` or "
    "`[tool arg '<key>' N bytes @ toolresult://... preview: ...]`. To see "
    "the full content, call `recall_tool_result(tool_call_id=<id>)` — pass "
    "the quoted key via `arg_key=` for the arg form. Fetched content lands "
    "inline in your next turn and counts toward the context budget on that "
    "turn, so recall only what the current step needs."
)


# ---------------------------------------------------------------------------
# Pydantic arguments
# ---------------------------------------------------------------------------


class RecallToolResultArguments(BaseModel):
    """LLM-facing argument schema.

    ``tool_call_id`` is the ``tooluse_*`` / ``call_*`` id that appeared in
    the placeholder. ``arg_key`` is only set when recalling a tool-call ARG
    (it is the quoted key in the ``[tool arg 'KEY' ...]`` placeholder).
    """

    # Allow extra keys so a provider that emits a legacy ``uri`` field
    # doesn't trip validation; we ignore them.
    model_config = ConfigDict(extra="ignore")

    tool_call_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=256,
            description=(
                "The `tool_call_id` of the offloaded tool call. Copy it "
                "from the `@ toolresult://...` placeholder's URI (third "
                "path component) or from the tool_calls list."
            ),
        ),
    ]
    arg_key: Annotated[
        str | None,
        Field(
            default=None,
            max_length=128,
            description=(
                "Only for arg-side recall. The quoted key from a "
                "`[tool arg '<key>' ...]` placeholder (e.g. 'content', "
                "'new_string', 'text'). Omit for tool-result recall."
            ),
        ),
    ] = None


# ---------------------------------------------------------------------------
# Error strings (exported so tests can assert without copy-paste drift)
# ---------------------------------------------------------------------------

ERROR_MALFORMED: str = "Error: not a valid tool result id"
ERROR_CROSS_TASK: str = (
    "Error: tool_call_id belongs to a different task or tenant"
)
ERROR_PURGED: str = (
    "Error: content not available (artifact may have been purged)"
)
ERROR_TRANSIENT: str = (
    "Error: artifact store temporarily unavailable; retry or continue "
    "without this content"
)
ERROR_AMBIGUOUS: str = (
    "Error: ambiguous tool_call_id — multiple offloaded versions exist; "
    "retry with a fresher tool_call_id from a recent placeholder"
)
ERROR_NO_HASH: str = (
    "Error: no offloaded content found for this tool_call_id (it may not "
    "have been offloaded, or has already been purged)"
)

# When NO_HASH fires, callers see this base string followed (on a newline)
# by diagnostic lines showing what they supplied and which ids ARE available
# in this task's artifact store — when any are found. The goal is to let the
# model self-correct its argument (e.g. it passed a stripped id without the
# ``tooluse_`` prefix) rather than silently guess for it. Production note:
# task 75f5a223 dead-lettered after the model called
# ``recall_tool_result(tool_call_id="vIEO28OtSiKBQVhMWM6KKb")`` when the real
# id was ``tooluse_vIEO28OtSiKBQVhMWM6KKb`` — we want that mistake to be
# obvious from the response string on the next turn.
_MAX_AVAILABLE_ENTRIES_IN_NO_HASH: int = 20


# ---------------------------------------------------------------------------
# Hash resolution
# ---------------------------------------------------------------------------


def _prefix_for(
    *, tenant_id: str, task_id: str, tool_call_id: str, arg_key: str | None
) -> str:
    """Return the S3 prefix under which the candidate objects live.

    Mirrors the key scheme from :mod:`executor.compaction.tool_result_store`:

    - result: ``{tenant_id}/{task_id}/{tool_call_id}/``
    - arg:    ``{tenant_id}/{task_id}/{tool_call_id}/args/{arg_key}/``
    """
    base = f"{tenant_id}/{task_id}/{tool_call_id}"
    if arg_key is None:
        return f"{base}/"
    return f"{base}/args/{arg_key}/"


def _single_hash_from_keys(keys: Iterable[str]) -> tuple[str | None, int]:
    """Extract the ``{hash}.txt`` file component from listed S3 keys.

    Returns ``(hash, count)``. Any key not ending in ``.txt`` is ignored so
    stray ``_manifest`` / ``.json`` companions don't trip the ambiguity
    check. ``count`` is the total number of ``.txt`` files observed.
    """
    hashes: list[str] = []
    for key in keys:
        # Accept bare filenames too (InMemoryToolResultStore in tests).
        leaf = key.rsplit("/", 1)[-1]
        if not leaf.endswith(".txt"):
            continue
        hashes.append(leaf[: -len(".txt")])
    # Dedupe because S3 versioning or manifest doubles would otherwise
    # report a false ambiguity.
    unique = list(dict.fromkeys(hashes))
    if not unique:
        return (None, 0)
    return (unique[0] if len(unique) == 1 else None, len(unique))


async def _list_task_offloaded_entries(
    store: ToolResultArtifactStore,
    *,
    tenant_id: str,
    task_id: str,
    limit: int = _MAX_AVAILABLE_ENTRIES_IN_NO_HASH,
) -> list[dict[str, Any]]:
    """Enumerate (tool_call_id, arg_key?) pairs present under this task.

    Returns an empty list when the store cannot list, when listing raises,
    or when nothing is offloaded. Bounded to ``limit`` entries so a task
    with hundreds of offloads does not produce a multi-KB error response.
    """
    prefix = f"{tenant_id}/{task_id}/"
    try:
        keys = await _list_prefix(store, prefix)
    except Exception:  # noqa: BLE001 — diagnostic-only, never raise
        return []
    if not keys:
        return []

    seen: set[tuple[str, str | None]] = set()
    entries: list[dict[str, Any]] = []
    for key in keys:
        # Strip the leading ``{tenant}/{task}/`` then look at path segments.
        rel = key[len(prefix):] if key.startswith(prefix) else key
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        tool_call_id = parts[0]
        arg_key: str | None = None
        # Arg form: {tool_call_id}/args/{arg_key}/{hash}.txt
        if len(parts) >= 4 and parts[1] == "args":
            arg_key = parts[2]
        k = (tool_call_id, arg_key)
        if k in seen:
            continue
        seen.add(k)
        entries.append({"tool_call_id": tool_call_id, "arg_key": arg_key})
        if len(entries) >= limit:
            break
    return entries


def _render_no_hash_with_diagnostics(
    *,
    supplied_tool_call_id: str,
    supplied_arg_key: str | None,
    available: list[dict[str, Any]],
) -> str:
    """Compose a NO_HASH response with echo + available-ids list.

    When ``available`` is empty we return ``ERROR_NO_HASH`` unchanged so
    callers asserting equality with the base constant keep working in the
    "nothing offloaded at all" case. Otherwise we append diagnostic lines
    echoing the caller-supplied id (so the model can spot its own mistake)
    plus the list of ids that DO exist in the store for this task.
    """
    if not available:
        return ERROR_NO_HASH
    lines: list[str] = [ERROR_NO_HASH]
    supplied_suffix = (
        f", arg_key={supplied_arg_key!r}" if supplied_arg_key else ""
    )
    lines.append(
        f"You supplied tool_call_id={supplied_tool_call_id!r}{supplied_suffix}."
    )
    lines.append("Available offloaded entries in this task:")
    for entry in available:
        tc = entry["tool_call_id"]
        arg = entry.get("arg_key")
        arg_suffix = f", arg_key={arg!r}" if arg else ""
        lines.append(f"  - tool_call_id={tc!r}{arg_suffix}")
    return "\n".join(lines)


async def _list_prefix(
    store: ToolResultArtifactStore, prefix: str
) -> list[str] | None:
    """Return keys under ``prefix`` or ``None`` when the store cannot list.

    Stores can expose either an ``async list_keys(prefix)`` / ``list(prefix)``
    method OR, in unit tests, a private ``_data`` dict keyed by URI. We prefer
    the public async API (production ``S3ToolResultStore`` below) and fall
    back to iterating ``_data`` keys for the in-memory test double without
    requiring callers to plumb a separate listing path.
    """
    list_fn: Callable[..., Any] | None = getattr(store, "list_keys", None) or getattr(
        store, "list", None
    )
    if list_fn is not None:
        result = list_fn(prefix)
        try:
            # Support both async and sync variants.
            if hasattr(result, "__await__"):
                return list(await result)
            return list(result)
        except Exception:  # noqa: BLE001 — any lister failure → raise, handled by caller
            raise
    # Fallback: in-memory store keeps its data dict keyed by canonical URI.
    inner = getattr(store, "_data", None)
    if isinstance(inner, dict):
        # URIs look like toolresult://{prefix}{hash}.txt — strip the scheme
        # for the prefix test.
        marker = "toolresult://" + prefix
        out: list[str] = []
        for uri in inner.keys():
            if uri.startswith(marker):
                out.append(uri[len("toolresult://") :])
        return out
    # No way to list — caller treats this as NO_HASH.
    return None


# ---------------------------------------------------------------------------
# Core resolution + fetch — factored out so tests can exercise without the
# full StructuredTool wrapper.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RecallContext:
    """Closure-bound per-task context for the recall tool.

    ``tenant_id`` and ``task_id`` come from the worker's task context at
    registration time — the LLM cannot override them via arguments. The
    ``store`` is the same ``ToolResultArtifactStore`` wired into the
    ingestion-offload path so there is one source of truth for what's in S3.
    """

    tenant_id: str
    task_id: str
    store: ToolResultArtifactStore


async def _resolve_and_fetch(
    *,
    ctx: _RecallContext,
    tool_call_id: str,
    arg_key: str | None,
) -> str:
    """Return the offloaded content string OR a user-facing Error: string.

    Never raises.
    """
    tool_call_id = (tool_call_id or "").strip()
    if not tool_call_id:
        # The Pydantic schema already enforces min_length=1; defence in depth
        # for LLMs that bypass schema validation.
        return ERROR_MALFORMED
    clean_arg_key = arg_key.strip() if isinstance(arg_key, str) and arg_key else None
    if clean_arg_key == "":
        clean_arg_key = None

    # Step 1 — list the S3 prefix to find the content-hash component.
    prefix = _prefix_for(
        tenant_id=ctx.tenant_id,
        task_id=ctx.task_id,
        tool_call_id=tool_call_id,
        arg_key=clean_arg_key,
    )
    try:
        keys = await _list_prefix(ctx.store, prefix)
    except Exception as e:  # noqa: BLE001 — listing surfaces transport failures
        logger.warning(
            "recall_tool_result.fetch_failed",
            extra={
                "tenant_id": ctx.tenant_id,
                "task_id": ctx.task_id,
                "tool_call_id": tool_call_id,
                "arg_key": clean_arg_key,
                "stage": "list",
                "error_type": type(e).__name__,
            },
        )
        return ERROR_TRANSIENT

    if keys is None:
        # Store does not support listing — treat as NO_HASH rather than
        # silently failing. In production S3ToolResultStore exposes list_keys;
        # tests either use the in-memory double or subclass.
        return ERROR_NO_HASH

    content_hash, count = _single_hash_from_keys(keys)
    if count == 0:
        # The id the model passed didn't match any offloaded entry.
        # Echo the input + list the ids that DO exist for this task so the
        # model can spot its own mistake (e.g. missing the ``tooluse_``
        # prefix) and retry with the correct argument. Diagnostic listing
        # is bounded; errors during listing are swallowed (this path is
        # already the error path — don't compound).
        available = await _list_task_offloaded_entries(
            ctx.store, tenant_id=ctx.tenant_id, task_id=ctx.task_id
        )
        return _render_no_hash_with_diagnostics(
            supplied_tool_call_id=tool_call_id,
            supplied_arg_key=clean_arg_key,
            available=available,
        )
    if content_hash is None:
        # Multiple distinct hashes — provider retry produced multiple
        # offloads. Rare; ask the agent to retry with a fresher id.
        logger.info(
            "recall_tool_result.ambiguous",
            extra={
                "tenant_id": ctx.tenant_id,
                "task_id": ctx.task_id,
                "tool_call_id": tool_call_id,
                "arg_key": clean_arg_key,
                "hash_count": count,
            },
        )
        return ERROR_AMBIGUOUS

    # Step 2 — reconstruct the canonical URI and validate.
    uri_obj = ToolResultURI(
        tenant_id=ctx.tenant_id,
        task_id=ctx.task_id,
        tool_call_id=tool_call_id,
        content_hash=content_hash,
        arg_key=clean_arg_key,
    )
    uri = uri_obj.to_uri()

    try:
        parsed = parse_tool_result_uri(uri)
    except ValueError:
        # Would only happen if tool_call_id / arg_key contain characters that
        # break the URI shape. Fail closed before touching the store.
        logger.warning(
            "recall_tool_result.malformed",
            extra={
                "tenant_id": ctx.tenant_id,
                "task_id": ctx.task_id,
                "tool_call_id": tool_call_id,
                "arg_key": clean_arg_key,
            },
        )
        return ERROR_MALFORMED

    # Belt-and-suspenders scope check. Since we reconstructed the URI from
    # the closure-bound tenant/task, this can only fail if the helper was
    # built with a tenant/task that itself differs from the parsed URI —
    # impossible today, but the gate keeps future refactors honest.
    if parsed.tenant_id != ctx.tenant_id or parsed.task_id != ctx.task_id:
        logger.warning(
            "recall_tool_result.cross_task_rejected",
            extra={
                "tenant_id": ctx.tenant_id,
                "task_id": ctx.task_id,
                "uri_tenant_id": parsed.tenant_id,
                "uri_task_id": parsed.task_id,
                "tool_call_id": tool_call_id,
                "arg_key": clean_arg_key,
            },
        )
        return ERROR_CROSS_TASK

    # Step 3 — fetch.
    try:
        content = await ctx.store.get(uri)
    except Exception as e:  # noqa: BLE001 — differentiate transport vs missing
        logger.warning(
            "recall_tool_result.fetch_failed",
            extra={
                "tenant_id": ctx.tenant_id,
                "task_id": ctx.task_id,
                "tool_call_id": tool_call_id,
                "arg_key": clean_arg_key,
                "stage": "get",
                "error_type": type(e).__name__,
            },
        )
        return ERROR_TRANSIENT

    if content is None:
        logger.info(
            "recall_tool_result.purged",
            extra={
                "tenant_id": ctx.tenant_id,
                "task_id": ctx.task_id,
                "tool_call_id": tool_call_id,
                "arg_key": clean_arg_key,
            },
        )
        return ERROR_PURGED

    logger.info(
        "recall_tool_result.served",
        extra={
            "tenant_id": ctx.tenant_id,
            "task_id": ctx.task_id,
            "tool_call_id": tool_call_id,
            "arg_key": clean_arg_key,
            "content_bytes": len(content.encode("utf-8")),
        },
    )
    return content


# ---------------------------------------------------------------------------
# StructuredTool factory
# ---------------------------------------------------------------------------


def build_recall_tool_result_tool(
    *,
    tenant_id: str,
    task_id: str,
    store: ToolResultArtifactStore,
) -> StructuredTool:
    """Return the ``recall_tool_result`` LangChain tool.

    Scope binding: ``tenant_id`` and ``task_id`` are captured by closure at
    graph-build time. The LLM cannot override them; ``arg_key`` and
    ``tool_call_id`` are the only LLM-supplied fields. The returned tool
    never raises out of its coroutine.
    """
    ctx = _RecallContext(tenant_id=tenant_id, task_id=task_id, store=store)

    async def recall_tool_result(
        tool_call_id: str, arg_key: str | None = None
    ) -> str:
        return await _resolve_and_fetch(
            ctx=ctx, tool_call_id=tool_call_id, arg_key=arg_key
        )

    return StructuredTool.from_function(
        coroutine=recall_tool_result,
        name=RECALL_TOOL_RESULT_NAME,
        description=RECALL_TOOL_RESULT_DESCRIPTION,
        args_schema=RecallToolResultArguments,
    )


__all__ = [
    "ERROR_AMBIGUOUS",
    "ERROR_CROSS_TASK",
    "ERROR_MALFORMED",
    "ERROR_NO_HASH",
    "ERROR_PURGED",
    "ERROR_TRANSIENT",
    "RECALL_TOOL_RESULT_DESCRIPTION",
    "RECALL_TOOL_RESULT_NAME",
    "RECALL_TOOL_RESULT_SYSTEM_PROMPT_HINT",
    "RecallToolResultArguments",
    "build_recall_tool_result_tool",
]
