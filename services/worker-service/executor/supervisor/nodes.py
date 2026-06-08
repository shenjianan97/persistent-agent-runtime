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

from core.subagent_events import emit_supervisor_iteration
from executor.supervisor.prompts import (
    build_brief_prompt,
    build_clarity_assessment_prompt,
    build_supervisor_prompt,
)
from executor.supervisor.state import SupervisorState
from executor.text import flatten_text as _flatten_text

logger = logging.getLogger(__name__)

# Supervisor loop decisions (the conditional-edge routing keys).
DECISION_CONTINUE = "continue"
DECISION_STOP = "stop"

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


async def scope_node(state: SupervisorState, config: RunnableConfig) -> dict:
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


# =========================================================================== #
# Phase 2 — supervisor_node (Task S6)
# =========================================================================== #


class SupervisorAllFailedError(RuntimeError):
    """Raised by ``supervisor_node`` only in the *zero-progress* case.

    Partial failure does NOT sink a Deep Research run (design "Partial subagent
    failure"): with ≥1 successful sub-agent the Supervisor proceeds (re-dispatch
    or stop). The graph fails **only** when a round returned **zero** successes
    AND the Supervisor cannot make progress (no successes accumulated across any
    round). Raising a typed error here lets the worker dead-letter the *one*
    task (Pattern A — there is no per-sub-agent task to fail) rather than loop
    forever on an all-failing fan-out.
    """


def _parse_supervisor_decision(content: Any) -> tuple[str, list[dict], str]:
    """Parse the Supervisor LLM output into ``(decision, raw_subtasks, reason)``.

    The Supervisor template asks for a single JSON object:
    ``{"decision": "continue"|"stop", "subtasks": [{"prompt": str,
    "subtask"?: str, "redispatch"?: bool}], "reason"?: str}``. Robust to fenced
    / surrounded JSON (reuses ``_loads_lenient``). On a parse failure we fail
    **safe to stop** with no subtasks — a Supervisor that cannot read its own
    decision must not spin up an unbounded fan-out; it routes to the Writer with
    whatever findings exist.

    NOTE: ``raw_subtasks`` are returned verbatim from the LLM here; the caller
    (``supervisor_node``) is what MINTS the stable ``subtask`` id — the id is
    NEVER trusted from this parse (§A11-E8). A ``subtask`` value present in the
    LLM emission is consulted ONLY as a re-dispatch carry-forward hint and only
    when ``redispatch`` is truthy.
    """
    text = _flatten_text(content).strip()
    obj = _loads_lenient(text)
    if not isinstance(obj, dict):
        logger.warning("supervisor.decision_unparseable falling_back_to_stop")
        return DECISION_STOP, [], "decision_unparseable"
    decision = str(obj.get("decision") or DECISION_STOP).strip().lower()
    if decision not in (DECISION_CONTINUE, DECISION_STOP):
        decision = DECISION_STOP
    raw = obj.get("subtasks")
    subtasks: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and str(entry.get("prompt") or "").strip():
                subtasks.append(entry)
    reason = str(obj.get("reason") or "").strip()
    return decision, subtasks, reason


def _split_success_failure(results: dict) -> tuple[int, int]:
    """Count (successes, failures) in the accumulated ``subagent_results``."""
    successes = sum(1 for r in results.values() if isinstance(r, dict) and r.get("ok"))
    failures = len(results) - successes
    return successes, failures


async def supervisor_node(state: SupervisorState, config: RunnableConfig) -> dict:
    """Phase 2 — decide subtasks, mint ids, set the loop decision (Task S6).

    Contract (plan §A4.1 S6):

    1. Read ``brief``, ``iteration``, and the accumulated ``subagent_results``.
    2. **Partial-failure gate (in-graph, never dead-letter unless zero progress).**
       If a prior round ran and returned **zero** successes — *and* no successes
       exist across any round — the run cannot make progress: raise
       ``SupervisorAllFailedError`` (the all-failed case alone; one flaky fetch
       must not sink the run — design "Partial subagent failure").
    3. **Iteration cap.** If ``iteration >= max_iterations`` force ``stop`` and
       emit a ``supervisor_iteration`` cap-reason event (route → Writer).
    4. LLM decision via the Supervisor template → ``(decision, raw_subtasks)``.
       ``stop`` → route to the Writer (S7). ``continue`` with no subtasks also
       degrades to ``stop`` (nothing to fan out).
    5. **Deterministic id minting (§A11-E8).** Each newly-emitted subtask's id
       is minted as ``f"{iteration}.{index}"`` — NEVER read from the LLM. A
       prior id is carried forward **only** on explicit re-dispatch (the LLM
       entry sets ``redispatch`` truthy *and* names an existing failed
       ``subtask``), so the reducer overwrites that marker in place. Within one
       round two emitted subtasks therefore always get distinct ids — a
       colliding LLM-chosen id cannot silently overwrite a sibling and lose its
       findings.
    6. **Clamp to ``max_fanout_per_iteration``** (truncate + cap-reason event —
       no silent truncation, §A7).
    7. Emit the ``supervisor_iteration`` decision event and return
       ``{iteration, subtasks, supervisor_decision}`` (the fan-out edge reads
       ``subtasks``; the iteration edge reads ``supervisor_decision``).

    Dependencies are read from ``config["configurable"]`` (same convention as
    ``scope_node``): ``supervisor_model`` (a chat model with ``ainvoke``),
    ``agent_config`` (the loaded snapshot), and ``supervisor_emit`` (the injected
    event sink — see ``core/subagent_events.py``).
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    model = configurable["supervisor_model"]
    agent_config: dict = configurable.get("agent_config") or {}
    supervisor_cfg: dict = agent_config.get("supervisor") or {}
    emit = configurable.get("supervisor_emit")
    max_fanout = int(supervisor_cfg.get("max_fanout_per_iteration") or 5)
    max_iterations = int(supervisor_cfg.get("max_iterations") or 3)

    brief = state.get("brief", "") or ""
    prev_iteration = int(state.get("iteration", 0) or 0)
    results: dict = dict(state.get("subagent_results") or {})
    successes, failures = _split_success_failure(results)

    # (2) Partial-failure gate — fail ONLY on zero progress (all-failed case).
    # A round ran (results non-empty) but produced no success anywhere → the run
    # cannot proceed. With ≥1 success we never raise; the Supervisor decides.
    if results and successes == 0:
        logger.warning(
            "supervisor.all_failed iteration=%s failures=%s — failing the run",
            prev_iteration,
            failures,
        )
        raise SupervisorAllFailedError(
            f"all {failures} sub-agent(s) failed in round {prev_iteration} "
            f"with no prior success — cannot make progress"
        )

    # (3) Iteration cap — force stop at the boundary (route → Writer).
    if prev_iteration >= max_iterations:
        reason = f"max_iterations ({max_iterations}) reached"
        logger.info("supervisor.iteration_cap %s", reason)
        await emit_supervisor_iteration(
            emit,
            iteration=prev_iteration,
            subtasks_emitted=0,
            decision=DECISION_STOP,
            reason=reason,
        )
        return {"supervisor_decision": DECISION_STOP, "subtasks": []}

    iteration = prev_iteration + 1

    # (4) LLM decision.
    decision_msg = await model.ainvoke(
        build_supervisor_prompt(
            brief,
            iteration=iteration,
            subagent_results=results,
        )
    )
    decision, raw_subtasks, llm_reason = _parse_supervisor_decision(
        decision_msg.content
    )

    if decision == DECISION_STOP or not raw_subtasks:
        # Nothing to fan out → route to the Writer. ``iteration`` is NOT bumped
        # (no round runs), so the cap math stays honest.
        await emit_supervisor_iteration(
            emit,
            iteration=prev_iteration,
            subtasks_emitted=0,
            decision=DECISION_STOP,
            reason=llm_reason,
        )
        return {"supervisor_decision": DECISION_STOP, "subtasks": []}

    # (6) Clamp BEFORE minting so ids are contiguous within the kept set.
    cap_reason = ""
    if len(raw_subtasks) > max_fanout:
        cap_reason = (
            f"max_fanout_per_iteration ({max_fanout}) clamp: "
            f"{len(raw_subtasks)} emitted, {max_fanout} kept"
        )
        logger.info("supervisor.fanout_clamp %s", cap_reason)
        raw_subtasks = raw_subtasks[:max_fanout]

    # (5) Deterministic id minting + carry-forward-on-re-dispatch.
    minted: list[dict] = []
    for index, entry in enumerate(raw_subtasks):
        carry = _carry_forward_id(entry, results)
        subtask_id = carry if carry is not None else f"{iteration}.{index}"
        minted.append({"subtask": subtask_id, "prompt": str(entry["prompt"])})

    # (7) Decision event (cap reason rides here when truncation happened).
    await emit_supervisor_iteration(
        emit,
        iteration=iteration,
        subtasks_emitted=len(minted),
        decision=DECISION_CONTINUE,
        reason=cap_reason or llm_reason,
    )

    logger.info(
        "supervisor.fanout iteration=%s subtasks=%s prior_successes=%s "
        "prior_failures=%s clamped=%s",
        iteration,
        len(minted),
        successes,
        failures,
        bool(cap_reason),
    )
    return {
        "iteration": iteration,
        "subtasks": minted,
        "supervisor_decision": DECISION_CONTINUE,
    }


def _carry_forward_id(entry: dict, results: dict) -> str | None:
    """Return a prior ``subtask`` id to carry forward, else ``None``.

    Carry-forward applies **only** on explicit re-dispatch: the LLM entry must
    set ``redispatch`` truthy AND name an existing ``subtask`` that currently
    holds a **failure** marker. Re-using a failed subtask's id makes the reducer
    overwrite its marker in place (idempotent update, never a duplicate —
    §A0 inv. 6). A ``subtask`` value WITHOUT ``redispatch`` is ignored: that is
    the §A11-E8 guard — the id is otherwise always minted, never trusted from
    the LLM, so two same-round subtasks cannot collide.
    """
    if not entry.get("redispatch"):
        return None
    candidate = str(entry.get("subtask") or "").strip()
    if not candidate:
        return None
    prior = results.get(candidate)
    if isinstance(prior, dict) and not prior.get("ok"):
        return candidate
    return None
