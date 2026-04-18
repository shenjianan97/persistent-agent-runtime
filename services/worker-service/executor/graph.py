"""LangGraph executor for agent tasks.

Builds and executes the LangGraph state machine with the given agent configuration.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable

import asyncpg
import executor.providers as providers
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from langchain_core.tools import StructuredTool
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langgraph.graph import StateGraph, MessagesState, START, END
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
from executor.memory_graph import (
    MemoryEnabledState,
    MEMORY_WRITE_NODE_NAME,
    PLATFORM_DEFAULT_SUMMARIZER_MODEL,
    SummarizerResult,
    effective_memory_enabled,
    memory_write_node,
)
from executor.embeddings import compute_embedding as _default_compute_embedding
from core.memory_repository import (
    count_entries_for_agent,
    max_entries_for_agent,
    pending_memory_log_preview,
    read_pending_memory_from_state_values,
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
from tools.errors import ToolExecutionError, ToolTransportError
from executor.mcp_session import McpToolCallError

logger = logging.getLogger(__name__)


def _handle_tool_error(e: Exception) -> str:
    """Route tool errors: re-raise infra failures for task-level retry,
    return user-fixable errors as messages so the LLM can self-correct."""
    if isinstance(e, (ToolTransportError, McpToolCallError)):
        raise e
    return f"Error: {e}\nPlease fix the error and try again."


class GraphExecutor:
    """Orchestrates LangGraph execution for a claimed task."""

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
        sandbox=None,
        s3_client=None,
    ) -> list[StructuredTool]:
        tools = []
        if "web_search" in allowed_tools:
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
            tools.append(StructuredTool.from_function(
                func=request_human_input,
                name="request_human_input",
                description=REQUEST_HUMAN_INPUT_TOOL.description,
                args_schema=RequestHumanInputArguments,
            ))

        if dev_task_controls_enabled() and "dev_sleep" in allowed_tools:
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
        memory_enabled: bool = False,
        task_input: str | None = None,
    ) -> StateGraph:
        """Assembles the LangGraph state machine and binds MCP tools."""
        provider = agent_config.get("provider", "anthropic")
        model_name = agent_config.get("model", "claude-3-5-sonnet-latest")
        temperature = agent_config.get("temperature", 0.7)
        allowed_tools = agent_config.get("allowed_tools", [])
        system_prompt = agent_config.get("system_prompt", "")
        sandbox_template = (agent_config.get("sandbox") or {}).get("template")

        # Build a separate platform system message with tool instructions
        platform_system_msg = self._build_platform_system_message(
            allowed_tools,
            injected_files=injected_files,
            sandbox_template=sandbox_template,
        )

        llm = await providers.create_llm(self.pool, provider, model_name, temperature)

        # Register built-in tools (pass sandbox and s3_client for sandbox tools)
        tools = self._get_tools(
            allowed_tools,
            cancel_event=cancel_event,
            task_id=task_id,
            tenant_id=tenant_id,
            sandbox=sandbox,
            s3_client=s3_client,
        )

        # Merge custom tools from MCP servers
        if custom_tools:
            tools = tools + custom_tools

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

        async def agent_node(state: MessagesState, config: RunnableConfig):
            messages = state["messages"]
            if not any(isinstance(m, SystemMessage) for m in messages):
                sys_messages = []
                if system_prompt:
                    sys_messages.append(SystemMessage(content=system_prompt))
                if platform_system_msg:
                    sys_messages.append(SystemMessage(content=platform_system_msg))
                messages = sys_messages + messages

            # Retry on rate limits inside the execution loop instead of
            # crashing and burning a task-level retry.
            max_rate_limit_retries = 5
            for attempt in range(max_rate_limit_retries + 1):
                try:
                    response = await self._await_or_cancel(
                        llm_with_tools.ainvoke(messages, config),
                        cancel_event,
                        task_id=task_id,
                        operation="agent",
                    )
                    return {"messages": [response]}
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
        # When ``memory.enabled`` AND NOT ``skip_memory_write``, we register a
        # custom state schema (MemoryEnabledState) which adds the
        # ``observations`` + ``pending_memory`` fields and attaches the
        # ``operator.add`` reducer on ``observations`` so the Task 7
        # ``memory_note`` tool can append durably. Memory-disabled agents
        # keep using the stock ``MessagesState`` — identical to pre-Track-5
        # behaviour.
        state_type = MemoryEnabledState if memory_enabled else MessagesState
        workflow = StateGraph(state_type)
        workflow.add_node("agent", agent_node)

        # Wire the ``memory_write`` node when memory is enabled. Placed on
        # the "no pending tool calls" branch so the terminal path becomes
        # ``agent → memory_write → END`` — HITL pauses, budget pauses, and
        # dead-letters exit the graph via different paths and therefore
        # never traverse this node.
        if memory_enabled:
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

        if tools:
            tool_node = ToolNode(tools, handle_tool_errors=_handle_tool_error)
            workflow.add_node("tools", tool_node)
            workflow.add_edge("tools", "agent")
            if memory_enabled:
                # ``tools_condition`` routes to ``tools`` when the agent
                # message has pending tool calls, else to the node we pass
                # as the third argument. Replace the "no pending calls"
                # target with ``memory_write`` so the single terminal path
                # out of ``agent`` runs the memory write.
                workflow.add_conditional_edges(
                    "agent",
                    tools_condition,
                    {"tools": "tools", END: MEMORY_WRITE_NODE_NAME},
                )
            else:
                workflow.add_conditional_edges("agent", tools_condition)
        else:
            if memory_enabled:
                workflow.add_edge("agent", MEMORY_WRITE_NODE_NAME)
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

        import json as _json
        await conn.execute(
            '''UPDATE checkpoints
               SET cost_microdollars = $1,
                   execution_metadata = $4::jsonb
               WHERE checkpoint_id = $2
                 AND task_id = $3::uuid''',
            cost_microdollars,
            checkpoint_id,
            task_id,
            _json.dumps(execution_metadata) if execution_metadata else None,
        )

        # 2. Insert into agent_cost_ledger
        await conn.execute(
            '''INSERT INTO agent_cost_ledger
                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
               VALUES ($1, $2, $3::uuid, $4, $5)''',
            tenant_id,
            agent_id,
            task_id,
            checkpoint_id,
            cost_microdollars,
        )

        # 3. Upsert agent_runtime_state, incrementing hour_window_cost_microdollars
        await conn.execute(
            '''INSERT INTO agent_runtime_state
                   (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
               VALUES ($1, $2, 0, $3, '1970-01-01T00:00:00Z', NOW())
               ON CONFLICT (tenant_id, agent_id) DO UPDATE
               SET hour_window_cost_microdollars = agent_runtime_state.hour_window_cost_microdollars + $3,
                   updated_at = NOW()''',
            tenant_id,
            agent_id,
            cost_microdollars,
        )

        # 4. Return cumulative task cost and hourly window cost
        cumulative_task_cost = await conn.fetchval(
            '''SELECT COALESCE(SUM(cost_microdollars), 0)
               FROM agent_cost_ledger
               WHERE task_id = $1::uuid''',
            task_id,
        )

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
                    checkpoint_id = await conn.fetchval(
                        '''SELECT checkpoint_id FROM checkpoints
                           WHERE task_id = $1::uuid AND checkpoint_ns = ''
                           ORDER BY created_at DESC LIMIT 1''',
                        task_id,
                    )
                    summarizer_cost = int(
                        pending_memory.get("summarizer_cost_microdollars") or 0
                    )
                    if checkpoint_id and summarizer_cost > 0:
                        await conn.execute(
                            '''INSERT INTO agent_cost_ledger
                                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                               VALUES ($1, $2, $3::uuid, $4, $5)''',
                            tenant_id, agent_id, task_id, checkpoint_id, summarizer_cost,
                        )
                        # Hourly-spend accrues normally — memory cost is
                        # exempt from the per-task pause check ONLY, not
                        # from the rolling-window aggregation.
                        await conn.execute(
                            '''INSERT INTO agent_runtime_state
                                   (tenant_id, agent_id, running_task_count,
                                    hour_window_cost_microdollars,
                                    scheduler_cursor, updated_at)
                               VALUES ($1, $2, 0, $3, '1970-01-01T00:00:00Z', NOW())
                               ON CONFLICT (tenant_id, agent_id) DO UPDATE
                               SET hour_window_cost_microdollars =
                                       agent_runtime_state.hour_window_cost_microdollars + $3,
                                   updated_at = NOW()''',
                            tenant_id, agent_id, summarizer_cost,
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
                        await conn.execute(
                            '''INSERT INTO agent_cost_ledger
                                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                               VALUES ($1, $2, $3::uuid, $4, $5)''',
                            tenant_id, agent_id, task_id, checkpoint_id,
                            embedding_cost,
                        )

                # Track 3: decrement running_task_count on completion.
                await conn.execute(
                    '''INSERT INTO agent_runtime_state
                           (tenant_id, agent_id, running_task_count,
                            hour_window_cost_microdollars, scheduler_cursor,
                            updated_at)
                       VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
                       ON CONFLICT (tenant_id, agent_id) DO UPDATE
                       SET running_task_count = GREATEST(
                               agent_runtime_state.running_task_count - 1, 0),
                           updated_at = NOW()''',
                    tenant_id, agent_id,
                )
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
    ) -> str:
        """Build platform-generated system message with tool instructions.

        This is injected as a separate SystemMessage, hidden from the customer's
        system prompt — similar to how Claude Code injects system context.
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

        # Phase 2 Track 5: single-source-of-truth memory gate — computed once
        # and consulted by graph assembly, the commit path, and the budget
        # carve-out. ``skip_memory_write`` lands as a typed column on the
        # tasks row (see migration 0011); the router should forward it via
        # ``task_data`` in Task 4's dispatch path.
        memory_enabled_for_task = effective_memory_enabled(
            agent_config=agent_config,
            skip_memory_write=bool(task_data.get("skip_memory_write", False)),
        )

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
                memory_enabled=memory_enabled_for_task,
                task_input=task_input,
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
                # For first run, inject HumanMessage based on initial input
                checkpoint_tuple = await checkpointer.aget_tuple(config)
                is_first_run = not checkpoint_tuple
                initial_input = {"messages": [HumanMessage(content=task_input)]} if is_first_run else None

                # Resume path: if this is a resumed task with a human response, use Command(resume=...)
                if not is_first_run:
                    human_response = await self.pool.fetchval(
                        'SELECT human_response FROM tasks WHERE task_id = $1::uuid', task_id
                    )
                    if human_response:
                        payload = json.loads(human_response)
                        # Decode the documented HITL resume payload
                        # {"kind":"follow_up","message":"..."} -> inject new HumanMessage
                        # {"kind":"input","message":"blue"} -> resume value is the message
                        # {"kind":"approval","approved":true} -> resume value is the payload itself
                        if payload.get("kind") == "follow_up":
                            # Follow-up: inject new HumanMessage into existing conversation
                            initial_input = {"messages": [HumanMessage(content=payload.get("message", ""))]}
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
                    if MEMORY_WRITE_NODE_NAME in event:
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
                                        checkpoint_id = await cost_conn.fetchval(
                                            '''SELECT checkpoint_id FROM checkpoints
                                               WHERE task_id = $1::uuid
                                               ORDER BY created_at DESC LIMIT 1''',
                                            task_id
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
                                                        await cost_conn.execute(
                                                            '''UPDATE checkpoints
                                                               SET execution_metadata = $1::jsonb
                                                               WHERE checkpoint_id = $2
                                                                 AND task_id = $3::uuid''',
                                                            json.dumps(execution_metadata),
                                                            checkpoint_id,
                                                            task_id,
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
                                                                    await sc_conn.execute(
                                                                        """INSERT INTO agent_cost_ledger
                                                                           (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                                                                           VALUES ($1, $2, $3::uuid, 'sandbox', $4)""",
                                                                        tenant_id, agent_id, task_id, pause_sandbox_cost,
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
                                            await sc_conn.execute(
                                                """INSERT INTO agent_cost_ledger
                                                   (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                                                   VALUES ($1, $2, $3::uuid, 'sandbox', $4)""",
                                                tenant_id, agent_id, task_id, hitl_sandbox_cost,
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
                                await cost_conn.execute(
                                    """INSERT INTO agent_cost_ledger
                                       (tenant_id, agent_id, task_id, checkpoint_id, cost_microdollars)
                                       VALUES ($1, $2, $3::uuid, 'sandbox', $4)""",
                                    tenant_id,
                                    agent_id,
                                    task_id,
                                    sandbox_cost_microdollars,
                                )
                                # Also roll sandbox cost into the last checkpoint so that
                                # total_cost_microdollars (summed from checkpoints by the API) includes it.
                                await cost_conn.execute(
                                    """UPDATE checkpoints
                                       SET cost_microdollars = cost_microdollars + $1
                                       WHERE checkpoint_id = (
                                           SELECT checkpoint_id FROM checkpoints
                                           WHERE task_id = $2::uuid AND checkpoint_ns = ''
                                           ORDER BY created_at DESC LIMIT 1
                                       )""",
                                    sandbox_cost_microdollars,
                                    task_id,
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

                if memory_enabled_for_task:
                    # Phase 2 Track 5: co-commit the memory UPSERT + FIFO
                    # trim + lease-validated task completion in one
                    # transaction. Read ``pending_memory`` from the final
                    # state values — the ``memory_write`` node just set it
                    # on the terminal branch.
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
                            "Task %s completed with memory (cost: %d microdollars, langfuse: %s).",
                            task_id, cumulative_task_cost, langfuse_status,
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
                            await conn.execute(
                                '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
                                   VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
                                   ON CONFLICT (tenant_id, agent_id) DO UPDATE
                                   SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                                       updated_at = NOW()''',
                                tenant_id, agent_id
                            )
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
            await self._handle_dead_letter(task_id, tenant_id, agent_id, "task_timeout", "Execution exceeded task logic timeout")
        except GraphRecursionError:
            await self._handle_dead_letter(task_id, tenant_id, agent_id, "max_steps_exceeded", f"Execution exceeded max_steps ({max_steps})")
        except GraphInterrupt as gi:
            await self._handle_interrupt(task_data, gi, worker_id)
        except LeaseRevokedException:
            # Lease was explicitly stripped before a checkpoint write
            logger.warning("Task %s raised LeaseRevokedException, stopping gracefully.", task_id)
            pass
        except Exception as e:
            # Step 4: Failure classification
            if self._is_retryable_error(e):
                await self._handle_retryable_error(task_data, e)
            else:
                await self._handle_dead_letter(task_id, tenant_id, agent_id, "non_retryable_error", str(e), error_code="fatal_error")
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
        if isinstance(e, ToolTransportError):
            return True
        if isinstance(e, (ConnectionError, TimeoutError)):
            return True

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
        hour_cost = await conn.fetchval(
            '''SELECT COALESCE(SUM(cost_microdollars), 0)
               FROM agent_cost_ledger
               WHERE tenant_id = $1 AND agent_id = $2
                 AND created_at > NOW() - INTERVAL '60 minutes' ''',
            tenant_id, agent_id
        )
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
            oldest_entry_time = await conn.fetchval(
                '''SELECT MIN(created_at) FROM agent_cost_ledger
                   WHERE tenant_id = $1 AND agent_id = $2
                     AND created_at > NOW() - INTERVAL '60 minutes' ''',
                tenant_id, agent_id
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
            await conn.execute(
                '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
                   VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
                   ON CONFLICT (tenant_id, agent_id) DO UPDATE
                   SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                       updated_at = NOW()''',
                tenant_id, agent_id
            )

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
                    await conn.execute(
                        '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
                           VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
                           ON CONFLICT (tenant_id, agent_id) DO UPDATE
                           SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                               updated_at = NOW()''',
                        tenant_id, agent_id
                    )
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

    async def _handle_retryable_error(self, task_data: dict[str, Any], e: Exception):
        task_id = str(task_data["task_id"])
        tenant_id = task_data.get("tenant_id", "default")
        agent_id = task_data.get("agent_id") or "unknown"
        retry_count = task_data.get("retry_count", 0)
        max_retries = task_data.get("max_retries", 3)
        worker_pool_id = self.config.worker_pool_id

        if retry_count >= max_retries:
            await self._handle_dead_letter(task_id, tenant_id, agent_id, "retries_exhausted", f"Max retries reached. Last error: {e}")
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
                await conn.execute(
                    '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
                       VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
                       ON CONFLICT (tenant_id, agent_id) DO UPDATE
                       SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                           updated_at = NOW()''',
                    tenant_id, agent_id
                )
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

    async def _handle_dead_letter(self, task_id: str, tenant_id: str, agent_id: str,
                                   reason: str, error_msg: str, error_code: str | None = None):
        worker_id = self.config.worker_id
        error_msg = str(error_msg)[:1024]
        effective_error_code = error_code or reason

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    '''UPDATE tasks
                       SET status='dead_letter',
                           dead_letter_reason=$1,
                           last_error_message=$2,
                           last_error_code=$3,
                           last_worker_id=$4,
                           dead_lettered_at=NOW(),
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
                if updated is not None:
                    # Track 3: Decrement running_task_count on dead-letter
                    await conn.execute(
                        '''INSERT INTO agent_runtime_state (tenant_id, agent_id, running_task_count, hour_window_cost_microdollars, scheduler_cursor, updated_at)
                           VALUES ($1, $2, 0, 0, '1970-01-01T00:00:00Z', NOW())
                           ON CONFLICT (tenant_id, agent_id) DO UPDATE
                           SET running_task_count = GREATEST(agent_runtime_state.running_task_count - 1, 0),
                               updated_at = NOW()''',
                        tenant_id, agent_id
                    )
                    await _insert_task_event(
                        conn, task_id, tenant_id, agent_id,
                        "task_dead_lettered", "running", "dead_letter",
                        worker_id, error_code=effective_error_code,
                        error_message=error_msg,
                        details={"dead_letter_reason": reason},
                    )

        if updated is None:
            logger.warning("Task %s dead-letter skipped: lease no longer owned by this worker.", task_id)
        else:
            logger.error("Task %s dead-lettered: %s (msg: %s)", task_id, reason, error_msg)


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
