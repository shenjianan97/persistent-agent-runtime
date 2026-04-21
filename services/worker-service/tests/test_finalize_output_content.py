"""Unit tests for ``executor.graph._finalize_output_content``.

This helper runs at the completion path of GraphExecutor and flattens the
final AIMessage's content for persistence under ``task.output.result``. The
checkpoint-persistence path (``langchain_dumps``) is untouched; only the
terminal artifact is normalized so the Console renders markdown without
provider-aware branching.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from executor.graph import _finalize_output_content


def test_finalize_empty_messages_returns_empty_string():
    assert _finalize_output_content([]) == ""


def test_finalize_plain_string_content_passes_through():
    msgs = [HumanMessage(content="hi"), AIMessage(content="final prose")]
    assert _finalize_output_content(msgs) == "final prose"


def test_finalize_anthropic_text_block_flattens():
    msgs = [AIMessage(content=[{"type": "text", "text": "answer"}])]
    assert _finalize_output_content(msgs) == "answer"


def test_finalize_openai_responses_shaped_content_flattens():
    """Regression: OpenAI-Responses AIMessages used to persist as a block
    list under output.result, and the Console fell back to rendering raw
    JSON instead of markdown. The write-time flattener must surface the
    prose.
    """
    openai_content = [
        {"id": "rs_1", "type": "reasoning", "summary": []},
        {
            "id": "msg_1",
            "type": "message",
            "content": [{"type": "output_text", "text": "Below is a summary"}],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "name": "web_search",
            "arguments": "{}",
        },
    ]
    msgs = [AIMessage(content=openai_content)]
    result = _finalize_output_content(msgs)
    assert isinstance(result, str)
    assert result == "Below is a summary"


def test_finalize_openai_native_output_text_flattens():
    msgs = [AIMessage(content=[{"type": "output_text", "text": "Here is the report"}])]
    assert _finalize_output_content(msgs) == "Here is the report"


def test_finalize_anthropic_multi_block_preserves_paragraph_breaks():
    """Regression for the separator bug Codex flagged — sibling text
    blocks for user-facing ``output.result`` must be joined with ``\\n\\n``,
    not ``""``. Without the separator, adjacent markdown headings collapse
    into the previous block's last line ("body.## Second heading"), which
    breaks rendering on the Console's Output card.

    This must also match the Java read-time normalizer
    (``MessageContentExtractor``) so legacy tasks and new tasks produce the
    same rendered output.
    """
    msgs = [
        AIMessage(
            content=[
                {"type": "text", "text": "## First heading\nFirst paragraph body."},
                {"type": "text", "text": "## Second heading\nSecond paragraph body."},
            ]
        )
    ]
    out = _finalize_output_content(msgs)
    assert out == (
        "## First heading\nFirst paragraph body."
        "\n\n"
        "## Second heading\nSecond paragraph body."
    )
    # Regression canary — the two headings must not be glued together.
    assert "body.## Second heading" not in out


def test_graph_completion_path_uses_finalize_helper():
    """Regression guard — the GraphExecutor completion branch must route
    the terminal message through ``_finalize_output_content``.

    A proper integration test would spin up GraphExecutor with a stubbed
    runtime; the surface area isn't worth it. Instead, verify at source
    level that (a) the helper is imported, and (b) the call site exists
    in the completion path. Reverting to ``messages[-1].content`` would
    fail this test.
    """
    import inspect
    from executor import graph as graph_module

    src = inspect.getsource(graph_module)
    assert "def _finalize_output_content" in src, (
        "helper definition missing from graph.py"
    )
    assert "output_content = _finalize_output_content(messages)" in src, (
        "graph.py completion path no longer routes through "
        "_finalize_output_content; output.result normalization regressed"
    )
    # Inverse assertion: the pre-fix idiom must NOT reappear near the
    # completion path.
    assert "messages[-1].content if messages" not in src, (
        "graph.py regressed to the pre-fix direct-access pattern; "
        "provider-shaped block lists will leak back into output.result"
    )
