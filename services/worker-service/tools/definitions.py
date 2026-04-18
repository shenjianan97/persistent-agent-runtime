"""Canonical Phase 1 MCP tool definitions and registration helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from langgraph.types import interrupt

from tools.calculator import MAX_EXPRESSION_LENGTH, evaluate_expression
from tools.memory_tools import (
    MemoryNoteArguments,
    MemorySearchArguments,
    TaskHistoryGetArguments,
    MEMORY_NOTE_DESCRIPTION,
    MEMORY_SEARCH_DESCRIPTION,
    TASK_HISTORY_GET_DESCRIPTION,
)
from tools.providers.search import DuckDuckGoSearchProvider, SearchProvider, SearchResult
from tools.read_url import ReadUrlFetcher
from tools.runtime_logging import get_tools_logger
from tools.upload_artifact import CreateTextArtifactArguments, CreateTextArtifactResult
from tools.sandbox_tools import (
    SandboxExecArguments,
    SandboxExecResult,
    SandboxReadFileArguments,
    SandboxReadFileResult,
    SandboxWriteFileArguments,
    SandboxWriteFileResult,
    ExportSandboxFileArguments,
    ExportSandboxFileResult,
)


SEARCH_QUERY = Annotated[
    str,
    Field(
        min_length=1,
        max_length=400,
        description="Search query string.",
    ),
]
SEARCH_MAX_RESULTS = Annotated[
    int,
    Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of search results to return.",
    ),
]
READ_URL_VALUE = Annotated[
    str,
    Field(
        min_length=1,
        max_length=2048,
        description="Public http or https URL to fetch.",
    ),
]
READ_URL_MAX_CHARS = Annotated[
    int,
    Field(
        default=5000,
        ge=500,
        le=20000,
        description="Maximum number of characters returned in extracted content.",
    ),
]
CALCULATOR_EXPRESSION = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_EXPRESSION_LENGTH,
        description="Arithmetic expression using only numeric literals and operators.",
    ),
]
DEV_SLEEP_SECONDS = Annotated[
    int,
    Field(
        default=10,
        ge=1,
        le=600,
        description="Number of seconds to sleep before returning.",
    ),
]


class WebSearchArguments(BaseModel):
    query: SEARCH_QUERY
    max_results: SEARCH_MAX_RESULTS = 5


class SearchResultModel(BaseModel):
    title: str
    url: str
    snippet: str


class WebSearchResult(BaseModel):
    provider: str
    query: str
    results: list[SearchResultModel]


class ReadUrlArguments(BaseModel):
    url: READ_URL_VALUE
    max_chars: READ_URL_MAX_CHARS = 5000


class ReadUrlResult(BaseModel):
    final_url: str
    title: str | None = None
    content: str


class CalculatorArguments(BaseModel):
    expression: CALCULATOR_EXPRESSION


class CalculatorResult(BaseModel):
    expression: str
    result: int | float


class RequestHumanInputArguments(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="The question or request to present to the human operator.",
    )


class RequestHumanInputResult(BaseModel):
    response: str


class DevSleepArguments(BaseModel):
    seconds: DEV_SLEEP_SECONDS = 10


class DevSleepResult(BaseModel):
    slept_seconds: int


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]


@dataclass(frozen=True)
class ToolDependencies:
    search_provider: SearchProvider
    read_url_fetcher: ReadUrlFetcher


WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description="Search the public web and return a bounded list of results.",
    input_model=WebSearchArguments,
    output_model=WebSearchResult,
)
READ_URL_TOOL = ToolDefinition(
    name="read_url",
    description="Fetch a public URL and return sanitized readable text.",
    input_model=ReadUrlArguments,
    output_model=ReadUrlResult,
)
CALCULATOR_TOOL = ToolDefinition(
    name="calculator",
    description="Evaluate a bounded arithmetic expression without using an LLM.",
    input_model=CalculatorArguments,
    output_model=CalculatorResult,
)
REQUEST_HUMAN_INPUT_TOOL = ToolDefinition(
    name="request_human_input",
    description="Request input from a human operator. The task will pause and wait for a human to respond.",
    input_model=RequestHumanInputArguments,
    output_model=RequestHumanInputResult,
)
DEV_SLEEP_TOOL = ToolDefinition(
    name="dev_sleep",
    description="Dev-only control tool that sleeps for a bounded duration before returning.",
    input_model=DevSleepArguments,
    output_model=DevSleepResult,
)
CREATE_TEXT_ARTIFACT_TOOL = ToolDefinition(
    name="create_text_artifact",
    description="Create an output artifact from inline text content. The file will be available for download via the API after task completion. Only use this when you need to produce a file from content you composed — if a sandbox is available, write the file there and use export_sandbox_file instead.",
    input_model=CreateTextArtifactArguments,
    output_model=CreateTextArtifactResult,
)
SANDBOX_EXEC_TOOL = ToolDefinition(
    name="sandbox_exec",
    description="Execute a shell command in the sandbox environment. Returns stdout, stderr, and exit code.",
    input_model=SandboxExecArguments,
    output_model=SandboxExecResult,
)
SANDBOX_READ_FILE_TOOL = ToolDefinition(
    name="sandbox_read_file",
    description="Read the content of a file from the sandbox filesystem. Returns the file content as text.",
    input_model=SandboxReadFileArguments,
    output_model=SandboxReadFileResult,
)
SANDBOX_WRITE_FILE_TOOL = ToolDefinition(
    name="sandbox_write_file",
    description="Write content to a file in the sandbox filesystem. Creates the file if it does not exist, overwrites if it does.",
    input_model=SandboxWriteFileArguments,
    output_model=SandboxWriteFileResult,
)
EXPORT_SANDBOX_FILE_TOOL = ToolDefinition(
    name="export_sandbox_file",
    description="Export a file from the sandbox and save it as an output artifact. The file will be available via the task artifacts API.",
    input_model=ExportSandboxFileArguments,
    output_model=ExportSandboxFileResult,
)


class MemoryNoteResult(BaseModel):
    """Result schema for the ``memory_note`` tool catalog entry."""

    ok: bool
    count: int


class MemorySearchResultSummary(BaseModel):
    memory_id: str
    title: str
    summary_preview: str | None = None
    outcome: str | None = None
    task_id: str | None = None
    created_at: str | None = None
    score: float | None = None


class MemorySearchResult(BaseModel):
    """Result schema for ``memory_search``. ``ranking_used`` is the ranking
    path the server actually executed (``hybrid`` | ``text`` | ``vector``)."""

    results: list[MemorySearchResultSummary]
    ranking_used: str | None = None


class TaskHistoryGetResult(BaseModel):
    """Bounded structured view returned by ``task_history_get``."""

    task_id: str
    agent_id: str
    input: str | None = None
    status: str
    final_output: str | None = None
    tool_calls: list[dict[str, Any]] = []
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    memory_id: str | None = None


# --- Phase 2 Track 5 built-in memory tools (Task 7) -----------------------
# Registered per-task from ``executor.graph`` via
# :func:`tools.memory_tools.build_memory_tools`. Included here so tooling
# that iterates the catalog (schema introspection, docs) can see them.
MEMORY_NOTE_TOOL = ToolDefinition(
    name="memory_note",
    description=MEMORY_NOTE_DESCRIPTION,
    input_model=MemoryNoteArguments,
    output_model=MemoryNoteResult,
)
MEMORY_SEARCH_TOOL = ToolDefinition(
    name="memory_search",
    description=MEMORY_SEARCH_DESCRIPTION,
    input_model=MemorySearchArguments,
    output_model=MemorySearchResult,
)
TASK_HISTORY_GET_TOOL = ToolDefinition(
    name="task_history_get",
    description=TASK_HISTORY_GET_DESCRIPTION,
    input_model=TaskHistoryGetArguments,
    output_model=TaskHistoryGetResult,
)

TOOL_DEFINITIONS = (WEB_SEARCH_TOOL, READ_URL_TOOL, CALCULATOR_TOOL)
TOOL_NAMES = tuple(definition.name for definition in TOOL_DEFINITIONS)
LOGGER = get_tools_logger()


def create_default_dependencies() -> ToolDependencies:
    return ToolDependencies(
        search_provider=DuckDuckGoSearchProvider(),
        read_url_fetcher=ReadUrlFetcher(),
    )


def get_tool_definitions() -> tuple[ToolDefinition, ...]:
    return TOOL_DEFINITIONS


def dev_task_controls_enabled() -> bool:
    return os.environ.get("APP_DEV_TASK_CONTROLS_ENABLED", "").lower() == "true"


def get_tool_definition(name: str) -> ToolDefinition:
    for definition in TOOL_DEFINITIONS:
        if definition.name == name:
            return definition
    raise KeyError(f"Unknown tool definition: {name}")


def get_tool_schema(name: str) -> dict[str, Any]:
    definition = get_tool_definition(name)
    schema = definition.input_model.model_json_schema()
    schema["title"] = f"{name}Arguments"
    return schema


def get_tool_output_schema(name: str) -> dict[str, Any]:
    definition = get_tool_definition(name)
    return definition.output_model.model_json_schema()


def register_tools(server: FastMCP, dependencies: ToolDependencies) -> None:
    @server.tool(
        name=WEB_SEARCH_TOOL.name,
        description=WEB_SEARCH_TOOL.description,
        structured_output=True,
    )
    async def web_search(
        query: SEARCH_QUERY,
        max_results: SEARCH_MAX_RESULTS = 5,
    ) -> WebSearchResult:
        LOGGER.info(
            "tool_call_started tool=%s query_length=%s max_results=%s",
            WEB_SEARCH_TOOL.name,
            len(query),
            max_results,
        )
        try:
            results = await dependencies.search_provider.search(query, max_results)
            payload = WebSearchResult(
                provider=dependencies.search_provider.provider_name,
                query=query,
                results=[
                    SearchResultModel(
                        title=result.title,
                        url=result.url,
                        snippet=result.snippet,
                    )
                    for result in results
                ],
            )
        except Exception:
            LOGGER.exception("tool_call_failed tool=%s", WEB_SEARCH_TOOL.name)
            raise
        LOGGER.info(
            "tool_call_succeeded tool=%s result_count=%s",
            WEB_SEARCH_TOOL.name,
            len(payload.results),
        )
        return payload

    @server.tool(
        name=READ_URL_TOOL.name,
        description=READ_URL_TOOL.description,
        structured_output=True,
    )
    async def read_url(
        url: READ_URL_VALUE,
        max_chars: READ_URL_MAX_CHARS = 5000,
    ) -> ReadUrlResult:
        LOGGER.info(
            "tool_call_started tool=%s url=%s max_chars=%s",
            READ_URL_TOOL.name,
            url,
            max_chars,
        )
        try:
            result = await dependencies.read_url_fetcher.fetch(url, max_chars)
            payload = ReadUrlResult(
                final_url=result.final_url,
                title=result.title,
                content=result.content,
            )
        except Exception:
            LOGGER.exception("tool_call_failed tool=%s url=%s", READ_URL_TOOL.name, url)
            raise
        LOGGER.info(
            "tool_call_succeeded tool=%s final_url=%s content_chars=%s",
            READ_URL_TOOL.name,
            payload.final_url,
            len(payload.content),
        )
        return payload

    @server.tool(
        name=CALCULATOR_TOOL.name,
        description=CALCULATOR_TOOL.description,
        structured_output=True,
    )
    async def calculator(expression: CALCULATOR_EXPRESSION) -> CalculatorResult:
        LOGGER.info(
            "tool_call_started tool=%s expression_length=%s",
            CALCULATOR_TOOL.name,
            len(expression),
        )
        try:
            payload = CalculatorResult(
                expression=expression,
                result=evaluate_expression(expression),
            )
        except Exception:
            LOGGER.exception("tool_call_failed tool=%s", CALCULATOR_TOOL.name)
            raise
        LOGGER.info(
            "tool_call_succeeded tool=%s result=%s",
            CALCULATOR_TOOL.name,
            payload.result,
        )
        return payload


def request_human_input(prompt: str) -> str:
    """Request input from a human operator. The task will pause until a response is provided."""
    response = interrupt({"type": "input", "prompt": prompt})
    return response


def normalize_search_results(results: list[SearchResult]) -> list[SearchResultModel]:
    return [
        SearchResultModel(title=item.title, url=item.url, snippet=item.snippet)
        for item in results
    ]
