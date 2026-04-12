"""LangGraph executor for agent tasks.

Builds and executes the LangGraph state machine with the given agent configuration.
"""

import asyncio
import json
import logging
import os
import re
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

from executor.mcp_session import McpSessionManager, ToolServerConfig, McpConnectionError
from executor.schema_converter import mcp_tools_to_structured_tools, MAX_TOOLS_PER_AGENT
from storage.s3_client import S3Client
from tools.definitions import (
    create_default_dependencies,
    WEB_SEARCH_TOOL,
    READ_URL_TOOL,
    CALCULATOR_TOOL,
    DEV_SLEEP_TOOL,
    REQUEST_HUMAN_INPUT_TOOL,
    UPLOAD_ARTIFACT_TOOL,
    WebSearchArguments,
    ReadUrlArguments,
    CalculatorArguments,
    DevSleepArguments,
    RequestHumanInputArguments,
    dev_task_controls_enabled,
    request_human_input,
)
from tools.calculator import evaluate_expression
from tools.errors import ToolExecutionError, ToolTransportError

logger = logging.getLogger(__name__)


class GraphExecutor:
    """Orchestrates LangGraph execution for a claimed task."""

    def __init__(self, config: WorkerConfig, pool: asyncpg.Pool):
        self.config = config
        self.pool = pool
        self.deps = create_default_dependencies()
        # Per-model cost rate cache: {model_name: (input_rate, output_rate)}
        self._cost_rate_cache: dict[str, tuple[int, int]] = {}
        s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL")
        s3_bucket_name = os.environ.get("S3_BUCKET_NAME", "platform-artifacts")
        self.s3_client = S3Client(
            endpoint_url=s3_endpoint_url,
            bucket_name=s3_bucket_name,
        )

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
            return {
                "host": row["host"],
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

        if "calculator" in allowed_tools:
            async def calculator(expression: str):
                return {"expression": expression, "result": evaluate_expression(expression)}
            tools.append(StructuredTool.from_function(
                coroutine=calculator,
                name="calculator",
                description=CALCULATOR_TOOL.description,
                args_schema=CalculatorArguments
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

        if "upload_artifact" in allowed_tools:
            from tools.upload_artifact import (
                UploadArtifactArguments,
                execute_upload_artifact,
            )

            async def upload_artifact(
                filename: str,
                content: str,
                content_type: str = "text/plain",
            ):
                return await execute_upload_artifact(
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
                    coroutine=upload_artifact,
                    name="upload_artifact",
                    description=UPLOAD_ARTIFACT_TOOL.description,
                    args_schema=UploadArtifactArguments,
                )
            )

        return tools

    async def _build_graph(
        self,
        agent_config: dict[str, Any],
        *,
        cancel_event: asyncio.Event,
        task_id: str,
        tenant_id: str = "default",
        custom_tools: list[StructuredTool] | None = None,
    ) -> StateGraph:
        """Assembles the LangGraph state machine and binds MCP tools."""
        provider = agent_config.get("provider", "anthropic")
        model_name = agent_config.get("model", "claude-3-5-sonnet-latest")
        temperature = agent_config.get("temperature", 0.7)
        allowed_tools = agent_config.get("allowed_tools", [])
        system_prompt = agent_config.get("system_prompt", "")

        # When HITL is enabled, append instruction so the LLM knows to use the tool
        if "request_human_input" in allowed_tools:
            hitl_instruction = (
                "\n\nYou have access to a `request_human_input` tool. "
                "When you need to ask the user a question, gather clarification, or request any input, "
                "you MUST call the `request_human_input` tool instead of writing questions in your response. "
                "The task will pause and wait for the user's answer before you continue."
            )
            system_prompt = system_prompt + hitl_instruction

        llm = await providers.create_llm(self.pool, provider, model_name, temperature)

        # Register built-in tools
        tools = self._get_tools(allowed_tools, cancel_event=cancel_event, task_id=task_id, tenant_id=tenant_id)

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
            if system_prompt and not any(isinstance(m, SystemMessage) for m in messages):
                messages = [SystemMessage(content=system_prompt)] + messages

            response = await self._await_or_cancel(
                llm_with_tools.ainvoke(messages, config),
                cancel_event,
                task_id=task_id,
                operation="agent",
            )
            return {"messages": [response]}

        # Define the Graph layout
        workflow = StateGraph(MessagesState)
        workflow.add_node("agent", agent_node)

        if tools:
            # LangGraph handles routing the LLM's ToolCall directly to our python functions
            tool_node = ToolNode(tools, handle_tool_errors=ToolExecutionError)
            workflow.add_node("tools", tool_node)
            workflow.add_edge("tools", "agent")
            workflow.add_conditional_edges("agent", tools_condition)
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
    ) -> tuple:
        """Record step cost in a single transaction.

        1. Update checkpoints.cost_microdollars for the given checkpoint_id
        2. INSERT into agent_cost_ledger
        3. UPSERT agent_runtime_state.hour_window_cost_microdollars (increment)
        4. Return (cumulative_task_cost, hourly_window_cost)
        """
        # 1. Update the checkpoint with the step cost
        await conn.execute(
            '''UPDATE checkpoints
               SET cost_microdollars = $1
               WHERE checkpoint_id = $2
                 AND task_id = $3::uuid''',
            cost_microdollars,
            checkpoint_id,
            task_id,
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
                custom_tools=custom_tools if custom_tools else None,
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
                nonlocal session_manager, per_task_langfuse_client
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
                async for event in compiled_graph.astream(initial_input, config=config, stream_mode="updates"):
                    # Step 6: Cancellation Awareness
                    if cancel_event.is_set():
                        logger.warning("Task %s cancelled or lease revoked during execution.", task_id)
                        return

                    # Per-checkpoint incremental cost tracking
                    if "agent" in event:
                        for ai_msg in event["agent"].get("messages", []):
                            if hasattr(ai_msg, 'response_metadata') and ai_msg.response_metadata:
                                try:
                                    step_cost, execution_metadata = await self._calculate_step_cost(
                                        ai_msg.response_metadata, model_name
                                    )
                                    if step_cost > 0:
                                        async with self.pool.acquire() as cost_conn:
                                            checkpoint_id = await cost_conn.fetchval(
                                                '''SELECT checkpoint_id FROM checkpoints
                                                   WHERE task_id = $1::uuid
                                                   ORDER BY created_at DESC LIMIT 1''',
                                                task_id
                                            )
                                            if checkpoint_id:
                                                try:
                                                    async with cost_conn.transaction():
                                                        cumulative_task_cost, hourly_cost = await self._record_step_cost(
                                                            cost_conn, task_id, tenant_id, agent_id, checkpoint_id, step_cost
                                                        )
                                                    logger.debug(
                                                        "Task %s step cost: %d microdollars (cumulative: %d, hourly: %d)",
                                                        task_id, step_cost, cumulative_task_cost, hourly_cost,
                                                    )
                                                except Exception:
                                                    logger.warning("Per-step cost recording failed for task %s", task_id, exc_info=True)
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
                                                        return  # Stop execution — task is now paused
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
                            return

                # Execution Finished successfully. Compute final output.
                messages = final_state.values.get("messages", [])
                output_content = messages[-1].content if messages else ""

                # Per-checkpoint cost tracking replaces end-of-task aggregation.
                # Costs are now written incrementally in the streaming loop above.

                # Step 5: Flush Langfuse traces before marking complete
                langfuse_status = "skipped"
                if per_task_langfuse_client is not None:
                    langfuse_status = await self._flush_langfuse_with_retry(per_task_langfuse_client, task_id)
                    per_task_langfuse_client = None  # Prevent double-flush in finally

                # Step 6: Completion Path
                output_data = {"result": output_content}
                if langfuse_endpoint_id:
                    output_data["langfuse_status"] = langfuse_status
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

    def _is_retryable_error(self, e: Exception) -> bool:
        """Determines if the exception should trigger a retry or immediate dead letter."""
        # Check exception type first (most reliable signal)
        if isinstance(e, ToolTransportError):
            return True
        if isinstance(e, (ConnectionError, TimeoutError)):
            return True

        error_str = str(e).lower()

        # 429 and 5xx are retryable — checked before string heuristics to avoid
        # false negatives (e.g. "invalid request rate exceeded" contains "invalid")
        if "429" in error_str or "rate limit" in error_str or "rate exceeded" in error_str:
            return True
        if re.search(r'\b50[0234]\b', error_str):
            return True

        # Non-retryable validation and client errors
        if "validation" in error_str or "invalid" in error_str or "unsupported" in error_str or "pydantic" in error_str:
            return False

        # 4xx HTTP responses (usually fatal)
        if re.search(r'\b40[0-4]\b', error_str):
            return False

        # For Phase 1, default unknown exceptions to non-retryable
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
