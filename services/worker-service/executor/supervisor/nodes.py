"""Supervisor topology graph nodes.

S5 implements **only** ``scope_node`` (phase 1). S6 appends ``supervisor_node``
(+ the ``Send`` fan-out) and S7 appends ``subagent_node`` / ``writer_node`` —
this module is kept additive for them.

``scope_node`` is the entry phase: it assesses the research query's clarity, —
*only when configured to* — asks the user one clarifying question (reusing the
existing ``waiting_for_input`` / ``interrupt()`` machinery), and produces the
immutable **brief** (the north star every later node reads). All node coroutines
follow the repo's ``async`` convention (see the ReAct nodes in
``executor/graph.py``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from executor.supervisor.prompts import (
    build_brief_prompt,
    build_clarity_assessment_prompt,
)

logger = logging.getLogger(__name__)

# v1 default for ``supervisor.scope_clarification_enabled`` when the agent
# config omits it (the ``research`` preset does NOT seed it — PresetDefaults
# leaves it ``null``). Default ON: clarification is the design intent (the
# LangChain Open Deep Research provenance — "users rarely provide sufficient
# context"); headless customers opt OUT by setting the flag ``false`` (plan §A8
# risk row). Documented v1 default, not a silent choice.
_DEFAULT_SCOPE_CLARIFICATION_ENABLED = True


def _extract_query(messages: list[BaseMessage]) -> str:
    """Pull the research query from the conversation.

    The query is the first human turn (the task input the worker seeds into the
    ``messages`` channel). ``messages`` is the declared ``RuntimeState`` channel
    the worker populates from ``task.input`` — reading it (not an undeclared
    ``query`` channel) is why the value survives LangGraph's input filtering.
    """
    for msg in messages:
        if getattr(msg, "type", None) == "human":
            return _flatten_text(msg.content)
    # Fallback: no explicit human turn — use whatever the first message carries.
    if messages:
        return _flatten_text(messages[0].content)
    return ""


def _flatten_text(content: Any) -> str:
    """Flatten LangChain message content (str | list[block]) to plain text.

    Mirrors ``executor/subagents/fanout._flatten_text`` — provider-shaped block
    lists carry text under ``{"type": "text", "text": ...}`` or bare
    ``{"text": ...}``; everything else stringifies. Read-boundary only.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _parse_assessment(content: Any) -> tuple[bool, str]:
    """Parse the clarity-assessment LLM output into ``(clear, question)``.

    The Scope template asks for ``{"clear": bool, "question"?: str}``. Robust to
    fenced / surrounded JSON. On any parse failure we **fail safe to clear** —
    a Scope phase that cannot read its own assessment must not strand the run on
    a phantom clarification; it proceeds to a best-effort brief.
    """
    text = _flatten_text(content).strip()
    obj = _loads_lenient(text)
    if not isinstance(obj, dict):
        logger.warning("scope.assessment_unparseable falling_back_to_clear")
        return True, ""
    clear = bool(obj.get("clear", True))
    question = str(obj.get("question") or "").strip()
    # An "ambiguous" verdict with no question is not actionable — treat as clear.
    if not clear and not question:
        return True, ""
    return clear, question


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Tolerate a JSON object embedded in prose / code fences.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def scope_node(state: dict, config: RunnableConfig) -> dict:
    """Phase 1 — assess clarity, conditionally clarify, produce the brief.

    Contract (plan §A4.1 S5):

    1. Read the research query from ``state["messages"]``.
    2. LLM clarity assessment via the Scope template.
    3. Conditional clarify gate honoring ``supervisor.scope_clarification_enabled``:
       * flag **true** + ambiguous → ``interrupt()`` (reusing ``waiting_for_input``);
         on ``Command(resume="<answer>")`` the resume value is the human answer,
         folded into brief generation.
       * flag **false** → **never** ``interrupt()`` under any assessment outcome;
         best-effort brief from the query alone.
       * flag true + clear → no interrupt; brief directly.
    4. Generate the brief (LLM) and write it ONCE into ``state["brief"]``;
       initialise ``iteration=0`` and ``subtasks=[]``.

    Dependencies are read from ``config["configurable"]`` (the same place the
    Supervisor graph's other nodes will read them — never re-fetched from the
    API): ``scope_model`` (a chat model with ``ainvoke``) and ``agent_config``
    (the already-loaded snapshot ``execute_task`` parses).
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    model = configurable["scope_model"]
    agent_config: dict = configurable.get("agent_config") or {}
    supervisor_cfg: dict = agent_config.get("supervisor") or {}
    flag = supervisor_cfg.get("scope_clarification_enabled")
    clarification_enabled = (
        _DEFAULT_SCOPE_CLARIFICATION_ENABLED if flag is None else bool(flag)
    )

    query = _extract_query(state.get("messages", []))

    # (2) Clarity assessment.
    assessment_msg = await model.ainvoke(build_clarity_assessment_prompt(query))
    clear, question = _parse_assessment(assessment_msg.content)

    # (3) Conditional clarify gate.
    asked_question: str | None = None
    answer: str | None = None
    if clarification_enabled and not clear:
        asked_question = question
        # Reuse the EXACT existing pause path (tools/definitions.py:449): the
        # worker maps a pending GraphInterrupt to the ``waiting_for_input`` task
        # status, and resumes with Command(resume="<answer>") whose value is the
        # human reply. No parallel pause channel is introduced.
        answer = interrupt({"type": "input", "prompt": question})
        if answer is not None:
            answer = _flatten_text(answer)
    # flag false → we never enter the branch above, so no interrupt() fires even
    # when the assessment judged the query ambiguous (best-effort brief).

    # (4) Brief generation — write-once. ``answer`` is folded in only on the
    # clarification path; otherwise the brief is synthesised from the query
    # alone (clear or headless).
    brief_msg = await model.ainvoke(
        build_brief_prompt(query, question=asked_question, answer=answer)
    )
    brief = _flatten_text(brief_msg.content).strip()

    logger.info(
        "scope.brief_generated clarification_enabled=%s clear=%s clarified=%s "
        "brief_chars=%s",
        clarification_enabled,
        clear,
        answer is not None,
        len(brief),
    )

    return {"brief": brief, "iteration": 0, "subtasks": []}
