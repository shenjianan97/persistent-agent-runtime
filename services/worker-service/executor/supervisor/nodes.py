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

import hashlib
import json
import logging
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from core.subagent_events import emit_subagent_finding, emit_supervisor_iteration
from executor.supervisor import citations
from executor.supervisor.cost import (
    UsageAccumulatingModel,
    merge_step_usage,
    usage_from_message,
)
from executor.supervisor.prompts import (
    build_brief_prompt,
    build_clarity_assessment_prompt,
    build_supervisor_prompt,
    build_writer_prompt,
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

    # S8 (§A11-E1) — surface this node's LLM spend through the cost channel so
    # the parent cost loop bills it additively to the parent super-step. Both
    # LLM calls (clarity assessment + brief) count.
    step_usage = merge_step_usage(
        usage_from_message(assessment_msg), usage_from_message(brief_msg)
    )
    return {
        "brief": brief,
        "iteration": 0,
        "subtasks": [],
        "step_usage": step_usage,
    }


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

    # S8 (§A11-E1) — the decision call's spend rides the cost channel.
    decision_usage = usage_from_message(decision_msg)

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
        return {
            "supervisor_decision": DECISION_STOP,
            "subtasks": [],
            "step_usage": decision_usage,
        }

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
        "step_usage": decision_usage,
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


# =========================================================================== #
# Phase 3/4 — subagent findings parse + one-shot Writer + citation binding (S7)
# =========================================================================== #

# D2 (DEFERRED — one-shot Writer reduction *algorithm*; design "Open decisions",
# plan §A8/D2, ledger §A12 row D2). This is the **open-decision flag** for the v1
# reduction. When the accumulated finding corpus exceeds this cap, the v1
# behavior is a HARD CAP: select the first ``WRITER_FINDINGS_CAP`` findings by
# the channel's append order (see ``reduce_findings`` for the ordering rationale),
# drop the rest, and ``log()`` the dropped ids + count (no silent truncation —
# §A7). Selection/reorder ONLY — ``supporting_quote`` is NEVER mutated (§A0 inv.4),
# so ``citations.resolve`` + ``citations.verify`` still work on the kept set.
#
# DEFERRED ALTERNATIVES (NOT built here — §"Open decisions"): map-reduce
# summarization of the corpus, or running Track-7 context compaction on the
# Writer input. v1 ships the cap + log; the better algorithm is gated on the S11
# cap-hit-rate + report-quality metrics (ledger D2). Tune this constant from that
# data — it is intentionally a single, clearly-commented knob.
WRITER_FINDINGS_CAP = 50


def _mint_finding_id(subtask: str, index: int, claim: str, quote: str) -> str:
    """Mint a stable, run-unique ``finding_id``.

    Deterministic (same subtask + index + claim + quote → same id, so a replayed
    super-step on resume re-mints identical ids and the append-only ``findings``
    channel stays idempotent under the projection's dedup) and scoped by
    ``subtask`` + ``index`` so two findings in the run never collide. The id is
    minted by the runtime, NEVER trusted from the model (mirrors the §A11-E8
    subtask-id discipline)."""
    digest = hashlib.sha1(
        f"{subtask}\x00{index}\x00{claim}\x00{quote}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{subtask}-{digest}"


def _parse_findings_payload(summary: Any) -> list[dict]:
    """Parse a sub-agent ``summary`` into raw ``{claim, source_url,
    supporting_quote}`` dicts (no ids yet).

    The Subagent template instructs a ``{"findings": [...]}`` JSON object as the
    final answer. Robust to fenced / surrounded JSON (reuses ``_loads_lenient``).
    On any parse failure → ``[]`` (a sub-agent that returned freeform prose
    contributes no structured findings — never raises into the graph). Only
    entries carrying a non-empty ``supporting_quote`` are kept (a finding with no
    quote cannot be cited or verified)."""
    text = _flatten_text(summary).strip()
    obj = _loads_lenient(text)
    if not isinstance(obj, dict):
        return []
    raw = obj.get("findings")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        quote = str(entry.get("supporting_quote") or "")
        if not quote.strip():
            continue
        out.append(
            {
                "claim": str(entry.get("claim") or "").strip(),
                "source_url": str(entry.get("source_url") or "").strip(),
                # Quote stored VERBATIM (no strip / normalize) — immutable (§A0
                # inv. 4); the verify pass + resolve read it exactly.
                "supporting_quote": quote,
            }
        )
    return out


async def parse_findings(
    summary: Any,
    *,
    iteration: int,
    subtask: str,
    emit: Any = None,
) -> list[dict]:
    """Parse one sub-agent's ``summary`` into structured findings + emit events.

    Returns a list of ``{finding_id, claim, source_url, supporting_quote}`` — the
    contract the Writer + ``citations`` consume. ``finding_id`` is minted by the
    runtime (``_mint_finding_id``), stable + run-unique, NEVER trusted from the
    model. Each finding emits a ``subagent_finding`` event
    ``{iteration, subtask, finding_id, source_url}`` via the S9 emit helper
    (claim / quote ride the Langfuse span, not the row — §A7).

    This is the bridge from S3's ``SubagentResult.summary`` (a freeform string)
    into the parent ``findings`` channel: S6's ``_fanout_node`` calls it on the
    successful sub-agent's summary and returns the result under ``{"findings":
    [...]}`` (the append-only channel S5 declared). A failed / empty sub-agent
    contributes ``[]`` and the ``subagent_results`` failure marker is unaffected.
    """
    raw = _parse_findings_payload(summary)
    findings: list[dict] = []
    for index, entry in enumerate(raw):
        finding_id = _mint_finding_id(
            subtask, index, entry["claim"], entry["supporting_quote"]
        )
        finding = {"finding_id": finding_id, **entry}
        findings.append(finding)
        await emit_subagent_finding(
            emit,
            iteration=iteration,
            subtask=subtask,
            finding_id=finding_id,
            source_url=entry["source_url"],
        )
    return findings


def reduce_findings(findings: list[dict]) -> list[dict]:
    """v1 Writer-context reduction — HARD CAP + log; select/reorder ONLY (D2).

    When ``len(findings) > WRITER_FINDINGS_CAP`` keep the FIRST
    ``WRITER_FINDINGS_CAP`` by the ``findings`` channel's natural **append
    order** (earliest iteration → earliest sub-agent → earliest finding, since
    the channel is ``operator.add`` and the fan-out appends per round) and drop
    the rest. Rationale for that ordering: append order is the recency/iteration
    order — earlier rounds laid the foundational findings the later rounds refine,
    so prefer-earliest keeps the corpus the Supervisor built on. (One documented
    ordering — see §"Open decisions" for the deferred richer alternatives.)

    Dropped findings are ``log()``ed (ids + count) — NO silent truncation (§A7).
    ``supporting_quote`` is NEVER mutated: this only selects a sub-list of the
    SAME finding dicts, so every kept quote is byte-identical to its input (§A0
    inv. 4, the core S7 invariant). Returns the input unchanged when under cap.
    """
    if len(findings) <= WRITER_FINDINGS_CAP:
        return findings
    kept = findings[:WRITER_FINDINGS_CAP]
    dropped = findings[WRITER_FINDINGS_CAP:]
    dropped_ids = [f.get("finding_id") for f in dropped]
    logger.info(
        "writer.findings_reduced cap=%s total=%s kept=%s dropped=%s dropped_ids=%s "
        "(D2 v1 hard-cap reduction — see WRITER_FINDINGS_CAP / design Open decisions)",
        WRITER_FINDINGS_CAP,
        len(findings),
        len(kept),
        len(dropped),
        dropped_ids,
    )
    return kept


async def writer_node(state: SupervisorState, config: RunnableConfig) -> dict:
    """Phase 4 — the ONE-SHOT Writer + citation binding (Task S7).

    Contract (plan §A4.1 S7 / design "Citation binding"):

    1. Reduce the accumulated ``findings`` to the Writer's context budget
       (``reduce_findings`` — v1 hard cap, select/reorder only, quotes immutable).
    2. ONE Writer LLM call over the reduced corpus + the ``brief``, honoring
       ``writer_style`` (``formal_report`` | ``annotated_bullets``). The Writer
       cites by ``finding_id`` ONLY — never inline source URLs (it cannot
       fabricate a source). This is a SINGLE call — NOT parallel section-writing
       (the design's load-bearing reason for one-shot).
    3. ``citations.verify`` — one verify LLM call per cited finding, run
       concurrently, flagging cited sentences whose finding quote does not
       support them (never rewrites the report / invents a source; a single
       flaky call degrades fail-safe for that one citation, not the report).
    4. ``citations.resolve`` each cited ``finding_id`` → a full citation at render
       (deterministic, no LLM; an unknown id → a render error flag, never a
       fabricated source).

    Returns ``{report, citations, verify_flags}`` for downstream rendering (S8
    wires this node as the iteration ``stop`` target; S9 renders the bound
    citations + verify flags on the Console).

    Dependencies are read from ``config["configurable"]`` (same convention as the
    other supervisor nodes): ``writer_model`` + ``verify_model`` (chat models with
    ``ainvoke``) and ``agent_config`` (the loaded snapshot, for ``writer_style``).
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    writer_model = configurable["writer_model"]
    verify_model = configurable.get("verify_model") or writer_model
    agent_config: dict = configurable.get("agent_config") or {}
    supervisor_cfg: dict = agent_config.get("supervisor") or {}
    writer_style = supervisor_cfg.get("writer_style")

    brief = state.get("brief", "") or ""
    all_findings: list[dict] = list(state.get("findings") or [])

    # (1) v1 reduction — cap + log; quotes immutable.
    reduced = reduce_findings(all_findings)

    # (2) One-shot Writer call.
    writer_msg = await writer_model.ainvoke(
        build_writer_prompt(brief, reduced, writer_style=writer_style)
    )
    report = _flatten_text(writer_msg.content)

    # (3) Verify pass — one concurrent call per cited finding (flags only; never
    # rewrites / fabricates; a flaky call degrades fail-safe per citation).
    # S8 (§A11-E1) — wrap the verify model so its per-citation call spend is
    # captured (``citations.verify`` returns only flags, discarding usage). This
    # avoids reshaping S7's verify contract.
    verify_wrapped = UsageAccumulatingModel(verify_model)
    verify_flags = await citations.verify(report, reduced, model=verify_wrapped)

    # (4) Resolve each cited id → a full citation (deterministic, no LLM).
    cited_ids = citations.extract_citations(report)
    resolved = [citations.resolve(fid, reduced) for fid in cited_ids]

    unsupported = sum(
        1 for f in verify_flags if f.get("supported") is False or f.get("error")
    )
    logger.info(
        "writer.report_generated findings_total=%s findings_used=%s cited=%s "
        "verify_flagged=%s writer_style=%s report_chars=%s",
        len(all_findings),
        len(reduced),
        len(cited_ids),
        unsupported,
        writer_style,
        len(report),
    )
    # S8 (§A11-E1) — surface writer + verify spend through the cost channel.
    step_usage = merge_step_usage(
        usage_from_message(writer_msg), verify_wrapped.usage
    )
    return {
        "report": report,
        "citations": resolved,
        "verify_flags": verify_flags,
        "step_usage": step_usage,
    }
