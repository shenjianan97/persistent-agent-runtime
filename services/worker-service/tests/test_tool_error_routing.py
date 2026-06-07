"""Regression: read_url transport failures surface to the agent, not task retry.

Production incident (task 2825e0ba…, 2026-06-07): the agent called
``read_url`` on a URL whose hostname failed to resolve. ``ReadUrlFetcher``
raised ``ToolTransportError``; ``_handle_tool_error`` re-raised it as an
infra failure, so the task was requeued at the task level. On every resume
the checkpointed pending tool call re-executed the same URL
deterministically (~100ms per attempt), burning all 10 task retries in
~20 minutes before dead-lettering with "Max retries reached. Last error:
Hostname could not be resolved for https://atb.nrel.gov/…". The agent never
saw the error, so it never adapted.

Decided behavior ("tell the agent, keep the task alive"): URL-level
failures are returned to the LLM as a correctable error ToolMessage. This
test drives the real compiled graph — ``GraphExecutor._build_graph`` with
the production ``ReadUrlFetcher`` whose resolver fails exactly as
production DNS did — and asserts:

1. the error lands in ``state["messages"]`` as a ToolMessage with
   ``status="error"`` and actionable content (URL included),
2. the NEXT model call happens (the agent loop continues), and
3. no exception escapes the graph.
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.config import WorkerConfig
from executor.graph import GraphExecutor
from executor.memory_graph import MemoryDecision
from tools.definitions import ToolDependencies
from tools.read_url import ReadUrlFetcher


FAILING_URL = "https://atb.nrel.gov/electricity/2024/index"


class _UnusedSearchProvider:
    provider_name = "unused"

    async def search(self, query: str, max_results: int):  # pragma: no cover
        raise AssertionError("web_search must not be called in this test")


class _ScriptedModel:
    """Fake chat model: returns scripted AIMessages in order, records calls."""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("scripted model ran out of responses")
        return self._responses.pop(0)


async def _failing_resolver(host: str, port: int) -> list[str]:
    """Resolver failing as production DNS did for the incident's hostname.

    EAI_NONAME == NXDOMAIN-style permanent failure — read_url must not even
    burn its single in-tool transient retry on it.
    """
    raise socket.gaierror(
        socket.EAI_NONAME, "nodename nor servname provided, or not known"
    )


@pytest.mark.asyncio
async def test_unresolvable_read_url_surfaces_to_agent_and_loop_continues():
    config = WorkerConfig(worker_id="test-worker", tenant_id="default")
    executor = GraphExecutor(
        config,
        MagicMock(),  # pool — only touched via best-effort/fallback paths here
        deps=ToolDependencies(
            search_provider=_UnusedSearchProvider(),
            read_url_fetcher=ReadUrlFetcher(resolver=_failing_resolver),
        ),
        s3_client=MagicMock(),
    )

    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "read_url",
                        "args": {"url": FAILING_URL, "max_chars": 2000},
                    }
                ],
            ),
            AIMessage(
                content="That source is unavailable; proceeding without it."
            ),
        ]
    )

    with patch("executor.providers.create_llm", AsyncMock(return_value=model)):
        workflow = await executor._build_graph(
            {
                "model": "claude-haiku-4-5",
                "allowed_tools": ["read_url"],
                # Keep the ToolNode wrapper a passthrough — no S3 in this test.
                "context_management": {"offload_tool_results": False},
            },
            cancel_event=asyncio.Event(),
            task_id="task-tool-error-routing",
            tenant_id="default",
            agent_id="agent-tool-error-routing",
            memory_decision=MemoryDecision(stack_enabled=False, auto_write=False),
            task_input="read the url",
        )

    graph = workflow.compile()

    # No exception may escape the graph — the production shape dead-lettered
    # exactly because one did.
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=f"read {FAILING_URL}")]},
        config={"recursion_limit": 10},
    )

    messages = result["messages"]

    # 1. The failure landed in state as an error ToolMessage the LLM can see.
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    tool_msg = tool_messages[0]
    assert tool_msg.status == "error"
    content = str(tool_msg.content)
    assert "could not be resolved" in content
    assert FAILING_URL in content

    # 2. The loop continued: the model was called again AFTER the tool error
    #    and its final answer is the last message.
    assert len(model.calls) == 2
    final = messages[-1]
    assert isinstance(final, AIMessage)
    assert "proceeding without it" in str(final.content)
