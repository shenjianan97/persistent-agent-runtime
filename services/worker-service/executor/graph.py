import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator

import asyncpg
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrock
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.errors import GraphRecursionError

from checkpointer.postgres import PostgresDurableCheckpointer, LeaseRevokedException
from core.config import WorkerConfig

from tools.definitions import (
    create_default_dependencies, 
    WEB_SEARCH_TOOL, 
    READ_URL_TOOL, 
    CALCULATOR_TOOL,
    DEV_SLEEP_TOOL,
    WebSearchArguments, 
    ReadUrlArguments, 
    CalculatorArguments,
    DevSleepArguments,
    dev_task_controls_enabled,
)
from tools.calculator import evaluate_expression

logger = logging.getLogger(__name__)


class GraphExecutor:
    """Orchestrates LangGraph execution for a claimed task."""
    
    def __init__(self, config: WorkerConfig, pool: asyncpg.Pool):
        self.config = config
        self.pool = pool
        self.deps = create_default_dependencies()

    def _get_tools(self, allowed_tools: list[str]) -> list[StructuredTool]:
        tools = []
        if "web_search" in allowed_tools:
            async def web_search(query: str, max_results: int = 5):
                results = await self.deps.search_provider.search(query, max_results)
                return [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]
            tools.append(StructuredTool.from_function(
                coroutine=web_search,
                name="web_search",
                description=WEB_SEARCH_TOOL.description,
                args_schema=WebSearchArguments
            ))
            
        if "read_url" in allowed_tools:
            async def read_url(url: str, max_chars: int = 5000):
                result = await self.deps.read_url_fetcher.fetch(url, max_chars)
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

        if dev_task_controls_enabled() and "dev_sleep" in allowed_tools:
            async def dev_sleep(seconds: int = 10):
                await asyncio.sleep(seconds)
                return {"slept_seconds": seconds}
            tools.append(StructuredTool.from_function(
                coroutine=dev_sleep,
                name="dev_sleep",
                description=DEV_SLEEP_TOOL.description,
                args_schema=DevSleepArguments
            ))
            
        return tools

    def _build_graph(self, agent_config: dict[str, Any]) -> StateGraph:
        """Assembles the LangGraph state machine and binds MCP tools."""
        model_name = agent_config.get("model", "claude-3-5-sonnet-latest")
        temperature = agent_config.get("temperature", 0.7)
        allowed_tools = agent_config.get("allowed_tools", [])
        system_prompt = agent_config.get("system_prompt", "")

        # Use Anthropic directly if model name has "claude"
        # Note: api_key passed explicitly because Python 3.14 breaks Pydantic V1's
        # env var auto-loading in langchain-anthropic.
        if "claude" in model_name.lower():
            llm = ChatAnthropic(
                model=model_name,
                temperature=temperature,
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            )
        else:
            llm = ChatBedrock(model_id=model_name, model_kwargs={"temperature": temperature})
            
        # Register the local MCP tools with the LLM 
        tools = self._get_tools(allowed_tools)
        if tools:
            llm_with_tools = llm.bind_tools(tools)
        else:
            llm_with_tools = llm

        async def agent_node(state: MessagesState, config: RunnableConfig):
            messages = state["messages"]
            if system_prompt and not any(isinstance(m, SystemMessage) for m in messages):
                messages = [SystemMessage(content=system_prompt)] + messages
                
            response = await llm_with_tools.ainvoke(messages, config)
            return {"messages": [response]}

        # Define the Graph layout
        workflow = StateGraph(MessagesState)
        workflow.add_node("agent", agent_node)
        
        if tools:
            # LangGraph handles routing the LLM's ToolCall directly to our python functions
            tool_node = ToolNode(tools)
            workflow.add_node("tools", tool_node)
            workflow.add_edge("tools", "agent")
            workflow.add_conditional_edges("agent", tools_condition)
        else:
            workflow.add_edge("agent", END)
            
        workflow.add_edge(START, "agent")
        return workflow

    async def execute_task(self, task_data: dict[str, Any], cancel_event: asyncio.Event) -> None:
        """Main entrypoint from the executor router."""
        task_id = str(task_data["task_id"])
        tenant_id = task_data["tenant_id"]
        agent_config = json.loads(task_data["agent_config_snapshot"])
        task_input = task_data["input"]
        max_steps = task_data.get("max_steps", 100)
        task_timeout_seconds = task_data.get("task_timeout_seconds", 3600)
        worker_id = self.config.worker_id
        
        pool = self.pool
        try:
            # 2. Init checkpointer
            checkpointer = PostgresDurableCheckpointer(
                pool,
                worker_id=worker_id,
                tenant_id=tenant_id
            )
            
            # 3. Build & Compile graph
            graph = self._build_graph(agent_config)
            compiled_graph = graph.compile(checkpointer=checkpointer)
            
            # 4. Config map
            model_name = agent_config.get("model", "claude-3-5-sonnet-latest")
            config = {
                "configurable": {
                    "thread_id": task_id,
                },
                "recursion_limit": max_steps,
            }
            
            async def run_astream():
                # For first run, inject HumanMessage based on initial input
                checkpoint_tuple = await checkpointer.aget_tuple(config)
                is_first_run = not checkpoint_tuple
                initial_input = {"messages": [HumanMessage(content=task_input)]} if is_first_run else None
                
                # Executing super-steps via astream
                async for event in compiled_graph.astream(initial_input, config=config, stream_mode="updates"):
                    # Step 6: Cancellation Awareness
                    if cancel_event.is_set():
                        logger.warning("Task %s cancelled or lease revoked during execution.", task_id)
                        return

                    await self._backfill_checkpoint_costs(task_id, model_name)

                if cancel_event.is_set():
                    return

                await self._backfill_checkpoint_costs(task_id, model_name)

                # Execution Finished successfully. Compute final output.
                final_state = await compiled_graph.aget_state(config)
                messages = final_state.values.get("messages", [])
                output_content = messages[-1].content if messages else ""
                
                # Step 5: Completion Path
                await pool.execute(
                    '''UPDATE tasks 
                       SET status='completed', 
                           output=$1, 
                           last_error_code=NULL,
                           last_error_message=NULL,
                           version=version+1,
                           lease_owner=NULL,
                           lease_expiry=NULL
                       WHERE task_id=$2::uuid''',
                    json.dumps({"result": output_content}),
                    task_id
                )
                logger.info("Task %s completed successfully.", task_id)

            # Step 2: Wrap execution in timeout
            await asyncio.wait_for(run_astream(), timeout=task_timeout_seconds)
            
        except asyncio.TimeoutError:
            await self._handle_dead_letter(task_id, "task_timeout", "Execution exceeded task logic timeout")
        except GraphRecursionError:
            await self._handle_dead_letter(task_id, "max_steps_exceeded", f"Execution exceeded max_steps ({max_steps})")
        except LeaseRevokedException:
            # Lease was explicitly stripped before a checkpoint write
            logger.warning("Task %s raised LeaseRevokedException, stopping gracefully.", task_id)
            pass
        except Exception as e:
            # Step 4: Failure classification
            if self._is_retryable_error(e):
                await self._handle_retryable_error(task_data, e)
            else:
                await self._handle_dead_letter(task_id, "non_retryable_error", str(e))

    def _extract_cost_from_stream_event(self, event: Any, model_name: str) -> int:
        usage = self._extract_usage_from_stream_event(event)
        if usage is None:
            return 0
        return self._calculate_cost_microdollars(model_name, usage)

    def _extract_cost_from_checkpoint_payload(self, checkpoint_payload: Any, model_name: str) -> int:
        if checkpoint_payload is None:
            return 0

        if isinstance(checkpoint_payload, str):
            try:
                checkpoint_payload = json.loads(checkpoint_payload)
            except json.JSONDecodeError:
                logger.warning("Unable to parse checkpoint payload while calculating cost.")
                return 0

        if not isinstance(checkpoint_payload, dict):
            return 0

        channel_values = checkpoint_payload.get("channel_values")
        if not isinstance(channel_values, dict):
            return 0

        messages = channel_values.get("messages")
        if not isinstance(messages, list) or not messages:
            return 0

        usage = self._extract_usage_from_message(messages[-1])
        if usage is None:
            return 0

        return self._calculate_cost_microdollars(model_name, usage)

    def _extract_usage_from_stream_event(self, event: Any) -> dict[str, int] | None:
        if not isinstance(event, dict):
            return None

        for update in event.values():
            usage = self._extract_usage_from_update(update)
            if usage is not None:
                return usage

        return None

    def _extract_usage_from_update(self, update: Any) -> dict[str, int] | None:
        if not isinstance(update, dict):
            return None

        messages = update.get("messages")
        if not isinstance(messages, list):
            return None

        for message in reversed(messages):
            usage = self._extract_usage_from_message(message)
            if usage is not None:
                return usage

        return None

    def _extract_usage_from_message(self, message: Any) -> dict[str, int] | None:
        usage = getattr(message, "usage_metadata", None)
        if usage is None and isinstance(message, dict):
            usage = message.get("usage_metadata")
            if usage is None:
                kwargs = message.get("kwargs")
                if isinstance(kwargs, dict):
                    usage = kwargs.get("usage_metadata")

        if not isinstance(usage, dict):
            return None

        input_tokens = self._coerce_usage_value(usage, "input_tokens", "prompt_tokens", "inputTokens")
        output_tokens = self._coerce_usage_value(usage, "output_tokens", "completion_tokens", "outputTokens")
        if input_tokens is None and output_tokens is None:
            return None

        return {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
        }

    def _coerce_usage_value(self, usage: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
        return None

    def _calculate_cost_microdollars(self, model_name: str, usage: dict[str, int]) -> int:
        pricing = self.config.model_pricing.get(model_name)
        if pricing is None:
            logger.warning("No pricing configured for model %s; checkpoint cost will remain zero.", model_name)
            return 0

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        input_cost = self._cost_from_tokens(input_tokens, pricing.input_microdollars_per_million)
        output_cost = self._cost_from_tokens(output_tokens, pricing.output_microdollars_per_million)
        return input_cost + output_cost

    def _cost_from_tokens(self, tokens: int, microdollars_per_million: int) -> int:
        return (tokens * microdollars_per_million + 500_000) // 1_000_000

    async def _persist_checkpoint_cost(self, task_id: str, checkpoint_id: str, pending_cost: int) -> None:
        await self.pool.execute(
            '''
            UPDATE checkpoints
            SET cost_microdollars = cost_microdollars + $1::int,
                metadata_payload = jsonb_set(
                    COALESCE(metadata_payload, '{}'::jsonb),
                    '{cost_microdollars}',
                    to_jsonb(COALESCE((metadata_payload->>'cost_microdollars')::bigint, 0) + $1::bigint),
                    true
                )
            WHERE task_id = $2::uuid AND checkpoint_id = $3
            ''',
            pending_cost,
            task_id,
            checkpoint_id,
        )

    async def _backfill_checkpoint_costs(self, task_id: str, model_name: str) -> None:
        rows = await self.pool.fetch(
            '''
            SELECT checkpoint_id, checkpoint_payload, cost_microdollars
            FROM checkpoints
            WHERE task_id = $1::uuid
              AND checkpoint_ns = ''
              AND cost_microdollars = 0
            ORDER BY thread_ts ASC
            ''',
            task_id,
        )

        if not rows:
            return

        for row in rows:
            pending_cost = self._extract_cost_from_checkpoint_payload(row["checkpoint_payload"], model_name)
            if pending_cost <= 0:
                continue
            await self._persist_checkpoint_cost(task_id, row["checkpoint_id"], pending_cost)

    def _is_retryable_error(self, e: Exception) -> bool:
        """Determines if the exception should trigger a retry or immediate dead letter."""
        error_str = str(e).lower()
        if "validation" in error_str or "invalid" in error_str or "unsupported" in error_str or "pydantic" in error_str:
            return False
        
        # 4xx HTTP responses (usually fatal)
        if "400" in error_str or "401" in error_str or "403" in error_str or "404" in error_str:
            return False

        # 429 and 5xx are retryable
        if "429" in error_str or "rate limit" in error_str:
            return True
        if any(code in error_str for code in ("500", "502", "503", "504")):
            return True
            
        if isinstance(e, (ConnectionError, TimeoutError)):
            return True
            
        # For Phase 1, default unknown exceptions to non-retryable 
        return False

    async def _handle_retryable_error(self, task_data: dict[str, Any], e: Exception):
        task_id = str(task_data["task_id"])
        retry_count = task_data.get("retry_count", 0)
        max_retries = task_data.get("max_retries", 3)
        worker_pool_id = self.config.worker_pool_id
        
        if retry_count >= max_retries:
            await self._handle_dead_letter(task_id, "retries_exhausted", f"Max retries reached. Last error: {e}")
            return
            
        new_retry_count = retry_count + 1
        backoff_seconds = min(300, 2 ** new_retry_count)
        retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
        
        error_msg = str(e)[:1024]
        async with self.pool.acquire() as conn:
            await conn.execute(
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
                   WHERE task_id=$4::uuid''',
                new_retry_count,
                retry_after,
                error_msg,
                task_id
            )
            # Re-queue notification
            await conn.execute("SELECT pg_notify('new_task', $1)", worker_pool_id)
        
        logger.info("Task %s hit retryable error. Requeued (try %d).", task_id, new_retry_count)

    async def _handle_dead_letter(self, task_id: str, reason: str, error_msg: str):
        worker_id = self.config.worker_id
        error_msg = str(error_msg)[:1024]
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''UPDATE tasks 
                   SET status='dead_letter', 
                       dead_letter_reason=$1, 
                       last_error_message=$2,
                       last_worker_id=$3,
                       dead_lettered_at=NOW(),
                       version=version+1,
                       lease_owner=NULL,
                       lease_expiry=NULL
                   WHERE task_id=$4::uuid''',
                reason,
                error_msg,
                worker_id,
                task_id
            )
        
        logger.error("Task %s dead-lettered: %s (msg: %s)", task_id, reason, error_msg)
