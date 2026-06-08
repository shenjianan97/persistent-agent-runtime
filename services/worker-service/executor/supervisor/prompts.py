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
