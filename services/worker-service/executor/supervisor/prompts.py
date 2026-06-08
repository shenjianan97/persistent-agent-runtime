"""Supervisor topology prompt templates.

S5 owns ONLY the **Scope** templates here (clarity assessment, clarification
question, brief generation). The Supervisor / Subagent / Writer templates are
S7's — this module is intentionally kept additive so S7 lands the rest without
a merge conflict. Do NOT pre-write those here.

The Scope phase is the design's adaptation of LangChain Open Deep Research's
scoping step: assess clarity *internally*, only ask the user when the query is
genuinely ambiguous, and produce a **brief** that "serves as our north star for
success" (design "Pattern provenance" → Scope row). Templates are module-level
string builders, matching how the repo holds prompts elsewhere.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Clarity assessment — does the query carry enough context to research?
# --------------------------------------------------------------------------- #
SCOPE_CLARITY_ASSESSMENT_PROMPT = """\
You are the scoping phase of a deep-research agent. Assess whether the user's \
research request below carries enough context to begin researching, or whether \
one clarifying question would materially improve the result.

Users rarely provide sufficient context in a research request, so prefer asking \
when a key dimension (scope, timeframe, audience, region, depth, or the specific \
decision the research supports) is genuinely missing or ambiguous. Do NOT ask \
about details you can reasonably assume or that would not change the research \
plan.

Respond with a single JSON object and nothing else:
  {{"clear": true}}                      — the request is clear enough to research
  {{"clear": false, "question": "..."}}  — ask exactly ONE clarifying question

Research request:
{query}
"""


# --------------------------------------------------------------------------- #
# Brief generation — the immutable north star.
# --------------------------------------------------------------------------- #
SCOPE_BRIEF_PROMPT = """\
You are the scoping phase of a deep-research agent. Write a concise research \
brief that will serve as the north-star goal anchor for the entire run — every \
later phase (planning, parallel sub-agents, and the final writer) refers back \
to it. State the objective, the key questions to answer, the intended scope and \
boundaries, and the form the final output should take. Do not begin researching; \
produce only the brief.

Original research request:
{query}
{clarification_block}
Write the research brief now."""


# Inserted into ``SCOPE_BRIEF_PROMPT`` only when a clarifying answer was folded
# in (clarification enabled + the user answered). Empty otherwise.
SCOPE_CLARIFICATION_FOLD_TEMPLATE = """\

Clarifying question asked of the user:
{question}

The user's answer (incorporate it into the brief):
{answer}
"""


# --------------------------------------------------------------------------- #
# Supervisor — iteration-decision protocol + structured subtask-emission (S6).
# --------------------------------------------------------------------------- #
# The Supervisor's output is PARSED into ``subtasks: [...]`` — never freeform
# prose (design "What the Supervisor topology owns"). The Supervisor decides per
# iteration how many subtasks to emit (dynamic within caps — design "Subagent
# count — dynamic, capped"); the runtime mints the stable ``subtask`` id, so the
# template deliberately does NOT ask the model to assign ids (§A11-E8). The only
# id the model may name is an existing FAILED subtask it wants re-dispatched,
# flagged ``redispatch: true``.
SUPERVISOR_DECISION_PROMPT = """\
You are the supervisor of a deep-research agent. Your north-star research brief \
is below. After each round of parallel sub-agents returns, you decide whether \
the research is complete or whether to dispatch another round of focused \
sub-tasks.

Research brief (the north star — refer back to it):
{brief}

Progress so far (sub-agent results keyed by sub-task id; ok=false marks a \
sub-agent that did not complete):
{results_block}

This is research round {iteration}. Decide:
- If the brief is sufficiently answered by the results so far, STOP (route to \
the writer).
- Otherwise, CONTINUE: decompose the remaining work into focused sub-tasks. \
Emit only as many as the work genuinely needs — each runs as an isolated \
parallel sub-agent with its own context.

Respond with a single JSON object and nothing else:
  {{"decision": "stop", "reason": "..."}}
  {{"decision": "continue",
    "subtasks": [
      {{"prompt": "focused instruction for one sub-agent"}},
      ...
    ],
    "reason": "..."}}

To retry a sub-task that failed in a prior round, include it with its existing \
id and a redispatch flag:
  {{"prompt": "...", "subtask": "<failed sub-task id>", "redispatch": true}}
Do NOT assign ids to brand-new sub-tasks — the runtime assigns them. Emit only \
the `prompt` for new sub-tasks."""


def _render_results_block(subagent_results: dict) -> str:
    """Render the accumulated results into a compact, readable block.

    Empty on the first round. Each line is ``<subtask>: <ok|FAILED:reason> —
    <summary>`` so the Supervisor can see what each sub-task produced or why it
    failed (the basis for a re-dispatch decision)."""
    if not subagent_results:
        return "(no results yet — this is the first round)"
    lines: list[str] = []
    for subtask, result in subagent_results.items():
        if isinstance(result, dict) and result.get("ok"):
            summary = str(result.get("summary") or "").strip()
            lines.append(f"- {subtask}: ok — {summary}")
        else:
            reason = (
                str(result.get("reason") or "error")
                if isinstance(result, dict)
                else "error"
            )
            lines.append(f"- {subtask}: FAILED ({reason})")
    return "\n".join(lines)


def build_supervisor_prompt(
    brief: str, *, iteration: int, subagent_results: dict | None = None
) -> str:
    """Render the Supervisor iteration-decision prompt for ``iteration``."""
    return SUPERVISOR_DECISION_PROMPT.format(
        brief=brief,
        iteration=iteration,
        results_block=_render_results_block(subagent_results or {}),
    )


def build_clarity_assessment_prompt(query: str) -> str:
    """Render the clarity-assessment prompt for ``query``."""
    return SCOPE_CLARITY_ASSESSMENT_PROMPT.format(query=query)


def build_brief_prompt(
    query: str, *, question: str | None = None, answer: str | None = None
) -> str:
    """Render the brief-generation prompt.

    When ``answer`` is provided (the clarification path), the asked ``question``
    and the user's ``answer`` are folded into the prompt so the brief reflects
    the resolved ambiguity. On the clear / headless path both are ``None`` and
    the brief is synthesised from ``query`` alone.
    """
    if answer:
        clarification_block = SCOPE_CLARIFICATION_FOLD_TEMPLATE.format(
            question=question or "", answer=answer
        )
    else:
        clarification_block = ""
    return SCOPE_BRIEF_PROMPT.format(query=query, clarification_block=clarification_block)
