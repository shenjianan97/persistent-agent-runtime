"""Current-date grounding in every supervisor-topology LLM prompt.

Regression for the live 2026-06-09 deep-research run (task 0729e3a3) that
titled its report "October 2024": none of the supervisor topology's prompts
carried the current date, so the model anchored relative timeframes ("the past
month") on its training-data present and sub-agents searched/hallucinated
2024-era sources. The ReAct path already injects "Today's date is YYYY-MM-DD."
via the platform SystemMessage (executor/graph.py); the supervisor's five
LLM-facing prompts must carry the same line.

No-infra, no-model — pure string assertions on the prompt builders.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from executor.supervisor.prompts import (
    build_brief_prompt,
    build_clarity_assessment_prompt,
    build_subagent_prompt,
    build_supervisor_prompt,
    build_writer_prompt,
)


def _expected_line() -> str:
    return f"Today's date is {datetime.now(timezone.utc).strftime('%Y-%m-%d')}."


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("clarity", lambda: build_clarity_assessment_prompt("recent Trump news")),
        ("brief", lambda: build_brief_prompt("recent Trump news")),
        (
            "brief+clarification",
            lambda: build_brief_prompt(
                "recent Trump news", question="Which region?", answer="US"
            ),
        ),
        (
            "supervisor",
            lambda: build_supervisor_prompt("the brief", iteration=1),
        ),
        ("subagent", lambda: build_subagent_prompt("find recent coverage")),
        (
            "writer",
            lambda: build_writer_prompt("the brief", []),
        ),
    ],
)
def test_prompt_carries_current_date(name: str, build) -> None:
    prompt = build()
    assert _expected_line() in prompt, (
        f"{name} prompt lacks current-date grounding — relative timeframes "
        f"will anchor on the model's training cutoff"
    )


def test_original_content_preserved() -> None:
    # The grounding line is additive — the role-defining openers the fake-model
    # routers key on must survive verbatim.
    assert "You are the scoping phase" in build_clarity_assessment_prompt("q")
    assert "You are the supervisor" in build_supervisor_prompt("b", iteration=1)
    assert "You are a focused research sub-agent" in build_subagent_prompt("s")
    assert "You are the writer" in build_writer_prompt("b", [])
