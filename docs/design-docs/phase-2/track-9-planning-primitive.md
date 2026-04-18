# Track 9 Design — Planning Primitive

**Status: Stub — direction sketched, full design pending its own brainstorm and design pass.**

## Why this track exists

Long-running agent tasks — coding loops especially, but also any task with more than a handful of steps — lose coherence when there is no durable surface for "what the agent is trying to accomplish and how far along it is". The LLM drifts, Track 7's compaction eventually removes planning detail from message history, worker restarts and follow-ups lose in-conversation scratch notes.

Claude Code's `TodoWrite` tool is the widely-copied answer to this gap. It works for a desktop CLI because the list lives in message history and Claude Code's context is small enough that the list stays visible. In a managed multi-tenant runtime with aggressive compaction and Console/API visibility requirements, the equivalent has to be **first-class durable state**, not a message-history convention.

Track 9 adds that primitive: a typed `plan` field on the task's graph state, written by the agent via a `plan_write` tool, auto-injected into the LLM's context on every call, and exposed to the Console and API.

## Relationship to other tracks

- **Track 5 (Memory):** Memory is *cross-task* ("what did I learn before?"). Plans are *within-task* ("what am I doing right now?"). Different scope, different storage, no shared schema. A task's plan is thrown away at task end; memory is curated and persisted.
- **Track 7 (Context Window Management):** The plan survives Track 7 compaction because it is injected *after* Track 7's transform runs, on every LLM call. Track 7 is free to drop old `plan_write` tool returns from history; the injection replaces that view.
- **Track 10 (Deep Research Mode, proposed):** Track 10 may choose a plan-execute graph topology (planner → executor → replanner) for research orchestration. If it does, it builds on Track 9's `plan` state field rather than inventing its own. **Track 9 is the data primitive; Track 10 picks the topology.**
- **Track 8 (Coding-Agent Primitives):** Coding agents are the motivating use case for the planning primitive. Track 8's out-of-scope list already flags "Todo/plan tracking tool — handled separately by Track 9".

## Shape of the proposed work

Direction established in brainstorm (2026-04-17):

1. **First-class durable state, not message-history convention.** Plan lives as a typed field on graph state (`plan: list[PlanItem]`), checkpointed by LangGraph, serves API reads, renders in Console.
2. **Per-task scope.** Fresh plan per task. No cross-task continuity at this primitive's layer — cross-task continuity is Track 5's job. Follow-up and redrive (Track 4) preserve the plan because they reuse the same `task_id`.
3. **ReAct, not plan-execute.** Graph topology is unchanged from today's single-agent-node ReAct loop. The agent decides when to plan and when to replan by calling `plan_write` with an updated list. No planner/executor/replanner nodes are introduced by Track 9. Track 10 may choose a different topology for its own mode; Track 9 does not force it.
4. **Auto-injection every LLM call, post-compaction.** Before each LLM call, if the plan is non-empty, a short rendered block (`"## Current plan\n☐ …\n✓ …"`) is prepended to the message list *after* Track 7's compaction transform runs. Guarantees the LLM always sees current plan state. Empty plan → no injection (no cost).
5. **Opt-in per agent via `agent_config.planning.enabled` (default `false`).** When `false`: no tool, no injection, no preamble. Matches Track 5's opt-in shape (`memory.enabled`).
6. **Platform-provided system-prompt preamble when enabled.** Prepended to the customer's `system_prompt` when `planning.enabled = true`. Tells the agent *when* to plan (non-trivial tasks) and *when not to* (1–2 step tasks). Directly addresses the "From Plan to Action" finding that forced bad plans hurt more than no plan.
7. **Item schema (Claude Code shape + id + result).** Each item is `{ id, content, activeForm, status, result }` where `status ∈ { pending, in_progress, completed, skipped }`. `id` is stable across plan rewrites so the Console can render progress diffs without heuristic matching. `result` is the agent-written outcome when an item moves to `completed` or `skipped` — audit trail for customer trust and compliance.

## Open design questions (to be answered during the brainstorm)

- **Write semantics.** Full-list replace (like Claude Code's `TodoWrite`) or patch-style (add/update/remove ops)? Full-replace is simpler but costs more tokens in the tool call; patch is cheaper but introduces the complexity of conflict resolution across turns.
- **Ownership.** Agent-only writes (simplest), customer-prescribed initial plan at submission (prescribes work like a job spec), or HITL-human edits during pauses (biggest surface, conflict resolution needed).
- **HITL integration.** Does transitioning an item to `in_progress` or `completed` trigger a HITL pause point? Or is the plan silent with respect to HITL and only the existing `wait_for_input` / `wait_for_approval` tools create pauses?
- **API surface.** Read-only (`GET /v1/tasks/{id}/plan`) or mutable (HITL plan edits via `PATCH`)? Tied to the ownership question.
- **Console rendering.** List view only? Timeline of status changes? Diff view across turns? How does it compose with the existing Unified Timeline from Track 2?
- **Plan size limits.** Platform cap on total items per plan, per-item content length, total tokens the injection can consume.
- **Rendering format for injection.** Markdown checkbox block? JSON? Rendered for max LLM parse-ability vs token economy.
- **Pre-compaction flush analogue.** Track 7 is expected to insert a memory flush hook before aggressive compaction. Does it also give the agent a chance to *update the plan* before aggressive compaction runs? The answer is probably "no — the plan is already durable and injected post-compaction", but worth confirming during design.
- **Exactly-one-`in_progress` rule.** Claude Code's `TodoWrite` prompt enforces "exactly one in_progress at a time". Do we enforce this at the tool layer (reject the call if more than one), at the prompt layer (discourage in the preamble), or leave it to the agent's discretion? Tool-layer enforcement is the safest but rejects legitimate cases where a coding agent might run tests in the background while editing.

## Next step

Run a dedicated brainstorm for this track (superpowers:brainstorming) before writing any implementation plan. The direction above is approved; the details in "Open design questions" are the ones that require decisions during the brainstorm.
