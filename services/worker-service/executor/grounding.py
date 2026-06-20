"""Shared prompt-grounding lines for LLM-facing prompt builders.

Every prompt assembled for a model call — the ReAct platform SystemMessage,
the supervisor topology's five phase prompts, and the ``dispatch_subagent``
sub-agent seed — must carry the same current-date grounding. Without it the
model anchors relative timeframes ("the past month") on its training-data
present: a live 2026-06 deep-research run titled its report "October 2024"
and its sub-agents hallucinated 2024-era source URLs. One definition here
keeps the wording identical everywhere (tests assert on it verbatim).
"""

from __future__ import annotations

from datetime import datetime, timezone


def today_line() -> str:
    """``Today's date is YYYY-MM-DD.`` (UTC), prefixed to LLM-facing prompts."""
    return f"Today's date is {datetime.now(timezone.utc).strftime('%Y-%m-%d')}."
