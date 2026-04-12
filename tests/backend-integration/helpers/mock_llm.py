import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, ToolCall


class DynamicChatProvider:
    """Builds mock LLM instances for the patched LLM factory."""

    def __init__(self, default_factory: Callable[[], MagicMock]):
        self._default_factory = default_factory
        self._factory: Callable[[], MagicMock] = default_factory
        self._queue: list[MagicMock] = []

    def set_factory(self, factory: Callable[[], MagicMock]) -> None:
        self._factory = factory
        self._queue = []

    def set_llm(self, llm: MagicMock) -> None:
        self._factory = lambda: llm
        self._queue = []

    def set_queue(self, llms: list[MagicMock]) -> None:
        self._queue = list(llms)

    def reset(self) -> None:
        self._factory = self._default_factory
        self._queue = []

    def build(self, *args: Any, **kwargs: Any) -> MagicMock:
        del args, kwargs
        if self._queue:
            return self._queue.pop(0)
        return self._factory()


def _new_mock() -> MagicMock:
    mock = MagicMock()
    mock.bind_tools.return_value = mock
    return mock


def simple_response(content: str = "Hello!") -> MagicMock:
    mock = _new_mock()
    mock.ainvoke = AsyncMock(return_value=AIMessage(content=content))
    return mock


def callback_friendly_response(content: str = "Hello!") -> FakeListChatModel:
    return FakeListChatModel(responses=[content])


def calculator_tool_call(expression: str = "2 + 2", final_answer: str = "The answer is 4.") -> MagicMock:
    call_msg = AIMessage(
        content="",
        tool_calls=[ToolCall(name="web_search", args={"query": expression}, id="call_1")],
    )
    final_msg = AIMessage(content=final_answer)
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[call_msg, final_msg])
    return mock


def retryable_then_success(error_msg: str = "503 Service Unavailable", final: str = "recovered") -> MagicMock:
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[Exception(error_msg), AIMessage(content=final)])
    return mock


def always_fails(error_msg: str = "400 Bad Request: invalid") -> MagicMock:
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=Exception(error_msg))
    return mock


def slow_response(delay: float = 999.0, content: str = "too late") -> MagicMock:
    async def _slow(*args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        await asyncio.sleep(delay)
        return AIMessage(content=content)

    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=_slow)
    return mock


def infinite_tool_loop(expression: str = "1+1") -> MagicMock:
    def _next(*args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        return AIMessage(
            content="",
            tool_calls=[ToolCall(name="web_search", args={"query": expression}, id=f"call_{id(object())}")],
        )

    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=_next)
    return mock


def tool_then_retryable_then_success(
    expression: str = "5*5",
    retryable_error: str = "503 Service Unavailable",
    final_answer: str = "done",
) -> MagicMock:
    call_msg = AIMessage(
        content="",
        tool_calls=[ToolCall(name="web_search", args={"query": expression}, id="call_resume")],
    )
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[call_msg, Exception(retryable_error), AIMessage(content=final_answer)])
    return mock


def tool_then_slow_final(expression: str = "5*5", delay: float = 30.0) -> MagicMock:
    call_msg = AIMessage(
        content="",
        tool_calls=[ToolCall(name="web_search", args={"query": expression}, id="call_lease")],
    )

    async def _slow(*args: Any, **kwargs: Any) -> AIMessage:
        del args, kwargs
        await asyncio.sleep(delay)
        return AIMessage(content="final")

    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[call_msg, _slow])
    return mock


def dev_sleep_tool_call(seconds: int = 10, final_answer: str = "done after sleep") -> MagicMock:
    call_msg = AIMessage(
        content="",
        tool_calls=[ToolCall(name="dev_sleep", args={"seconds": seconds}, id="call_dev_sleep")],
    )
    final_msg = AIMessage(content=final_answer)
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[call_msg, final_msg])
    return mock


def upload_artifact_call(filename: str, content: str, content_type: str = "text/plain",
                         final_answer: str = "Artifact uploaded successfully.") -> MagicMock:
    """Create a mock LLM that calls upload_artifact, then responds with a final answer."""
    call_msg = AIMessage(
        content="",
        tool_calls=[ToolCall(
            name="upload_artifact",
            args={
                "filename": filename,
                "content": content,
                "content_type": content_type,
            },
            id="call_upload_artifact",
        )],
    )
    final_msg = AIMessage(content=final_answer)
    mock = _new_mock()
    mock.ainvoke = AsyncMock(side_effect=[call_msg, final_msg])
    return mock
