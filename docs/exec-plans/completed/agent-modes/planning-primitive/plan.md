# Agent Modes — Track: Planning Primitive

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **One of two tracks under [Agent Modes](../../../active/agent-modes/README.md).** Sibling track: [Supervisor Topology](../../../active/agent-modes/supervisor-topology/plan.md) (independent; shares only `RuntimeState`, the worker tool registry, and the Console Activity pane — see *Cross-track coordination* in §A3).

**Goal:** Ship the **Planning Primitive** — a ReAct agent's own to-do-list "scratchpad": a `plan` field on the runtime state written by a `plan_write` tool, injected back into the prompt **after** Track-7 compaction so it survives, exposed read-only at `GET /v1/tasks/{id}/plan`, and rendered as a checklist on the Console task-detail Activity pane. Governing design: [Agent Modes design](../../../../design-docs/agent-modes/design.md) → *How Planning Primitive composes*.

**Architecture:** Small, self-contained, over the existing single-graph worker runtime. The plan is **agent-owned scratchpad state** (the design's resolution of the original Track-9 ownership question): the agent rewrites it freely via `plan_write`; the platform never mutates it and never gates HITL on plan transitions. It lives in the LangGraph checkpoint (a new `RuntimeState.plan` channel), is injected as a neutral `SystemMessage` at a stable post-compaction position so the KV-cache prefix is preserved, and is surfaced read-only — there is **no plan-mutation API**. "Exactly one `in_progress`" is **prompt-layer guidance**, not tool-layer rejection.

**Tech Stack:** Python + LangGraph 1.0.5 (worker); Spring Boot / Jackson (read endpoint); React 19 / TypeScript / Vite (Console); PostgreSQL (plan lives in the existing JSONB checkpoint — no migration); no new tables or columns.

---

## A0. Design Contract & Decided Points

**Canonical design:** [`docs/design-docs/agent-modes/design.md`](../../../../design-docs/agent-modes/design.md) → *How Planning Primitive composes* and *What this resolves from the original Track 9 open-questions list*. Decided (not open):

1. **Agent-only ownership.** The Planning Primitive owns *only* the ReAct agent scratchpad. Customer-prescribed plans go through the (Phase-3) Workflow resource; the Supervisor's internal research plan is a *different* schema in the sibling track. They share Console UX conventions (a checklist) but **not** data structures.
2. **Read-only API.** `GET /v1/tasks/{id}/plan` only. Plan mutation is not exposed; the agent mutates it in-band via `plan_write`.
3. **No HITL on plan transitions.** The existing `waiting_for_input` / `waiting_for_approval` states handle pauses; item transitions never pause.
4. **Exactly-one-`in_progress` is prompt-layer guidance,** not tool-layer rejection — the plan is a self-reminder, not load-bearing state.
5. **Injected post-compaction.** The plan is durable and re-injected *after* the Track-7 compaction hook so it survives Tier 1/3, mirroring the Track-7 pre-Tier-3 memory-flush precedent. Neutral framing (silent-compaction rule) — never "you are being compacted."

**Resolutions of the design's "stays open for its own design pass" list** (this plan *is* that pass):
- **Write semantics:** full-list replace (Claude Code `TodoWrite` shape), not patch ops. *(P1)*
- **Injection format:** Markdown checkbox list. *(P2)*
- **Size limits:** v1 caps — **50 items, 200-char titles** (P1 documents the exact constants).
- **Pre-compaction flush interaction with Track 7:** none — the plan is durable + injected post-compaction, so no pre-compaction flush hook.

---

## A1. Implementation Overview

1. **`plan` state + `plan_write` tool (P1, worker).** Add a `plan` field to `RuntimeState` (`executor/compaction/state.py`) — a list of items `{id, title, status}` (`status ∈ pending|in_progress|completed`) with a **full-list-replace reducer**. A `plan_write` built-in tool (new `tools/plan_tools.py`) registered in `_get_tools`, allowlist-gated, writes the plan verbatim into state. v1 caps (50 items / 200-char titles). The tool does **not** enforce one-`in_progress` (that is prompt-layer, P2).
2. **Post-compaction plan injection (P2, worker).** After `compaction_pre_model_hook` returns and **before** `apply_cache_markers`, inject the current plan as a neutral `SystemMessage` (Markdown checklist), so it survives Tier 1/3 compaction. Empty plan → no injection (no-op). Position the block in the **uncached suffix** (after the last cache breakpoint) so a *plan change* invalidates the minimum cache span (see §A4). The one-`in_progress` guidance lives in the injected preamble text.
3. **Read-only plan API (P3, API).** `GET /v1/tasks/{id}/plan` → `TaskPlanResponse { task_id, plan: [{id,title,status}], updated_at }`, projected from the latest checkpoint's `plan` channel (mirrors the `GET /v1/tasks/{id}/activity` sub-resource + `ActivityProjectionService` pattern). 404 if the task is absent; `{plan: []}` when no plan channel exists (including supervisor-topology tasks, which never write one).
4. **Console plan checklist (P4, Console).** Render the plan as a checkbox list on the task-detail Activity pane (or a new `PlanChecklist.tsx`), fetched via a new `api.getTaskPlan` client method. `data-testid="plan-item-{id}"` + a status badge.
5. **Integration + browser tests (P5).** Plan persists across a Tier-1/Tier-3 compaction (injected block present post-compaction); `GET /v1/tasks/{id}/plan` shape (populated / empty / 404); checklist renders. Playwright scenario text shipped; the orchestrator runs it.

---

## A2. Impacted Components

| Component | Path | Change |
|---|---|---|
| `plan` state + `plan_write` | `services/worker-service/executor/compaction/state.py`, new `services/worker-service/tools/plan_tools.py`, `executor/graph.py` (`_get_tools`) | new + mod |
| Plan injection | `services/worker-service/executor/graph.py` (`agent_node`, post-`compaction_pre_model_hook`) | mod |
| Plan read API | `services/api-service/.../controller/TaskController.java`, new `service/TaskPlanService.java`, new `model/response/TaskPlanResponse.java` | new + mod |
| Console plan checklist | `services/console/src/features/task-detail/ActivityPane.tsx` (or new `PlanChecklist.tsx`), `api/client.ts`, `types/index.ts` | new + mod |
| Tests | worker `tests/`, `tests/backend-integration/`, Console unit + `CONSOLE_BROWSER_TESTING.md` scenarios | new |

**Verified anchors (cite these in tasks):** `RuntimeState` + reducers (`_max_reducer`, `_any_reducer`, `_list_replace_reducer`, `add_messages`) in `services/worker-service/executor/compaction/state.py`; tool registration `_get_tools` `executor/graph.py:850-1057`, `llm.bind_tools` `:1269`, `MAX_TOOLS_PER_AGENT` `:1262`; `agent_node` `:1344`; compaction hook call `compaction_pre_model_hook` `:1383`; cache markers `apply_cache_markers` `:1404`; the pre-Tier-3 memory-flush injection precedent (Track 7). API: sub-resource pattern `GET /v1/tasks/{taskId}/activity` `TaskController.java:190` + `ActivityProjectionService` (projects the latest checkpoint's channel values via Jackson). Console: `features/task-detail/ActivityPane.tsx`, `api/client.ts` `listActivity` `:249`, `types/index.ts` `ActivityEvent` `:475`.

---

## A3. Dependency Graph

```
  P1 (plan state + plan_write tool) ─┬─► P2 (post-compaction injection)
                                     └─► P3 (GET /v1/tasks/{id}/plan read API)
  P1, P2, P3 ──► P4 (Console plan checklist) ──► P5 (integration + browser)
```

- **P1 is the blocker** — it defines the `plan` item shape `{id, title, status}` that P2 injects, P3 returns, and P4 renders. Land it first.
- **P2 ∥ P3** after P1 — P2 is worker (`graph.py`), P3 is Java API, zero overlap.
- **P3 (Java) ∥ P1/P2 (Python)** — P3 only needs the plan item *shape*, which is fixed by P1's contract; it can start as soon as that shape is agreed.

**Cross-track coordination (with the [Supervisor Topology](../../../active/agent-modes/supervisor-topology/plan.md) track):** the two tracks are independent but touch four shared files — `executor/compaction/state.py` (both add a `RuntimeState` field), `executor/graph.py::_get_tools` (both register a built-in tool), `services/console/src/features/task-detail/ActivityPane.tsx`, and `types/index.ts`. **If both tracks run concurrently, any agent editing one of these MUST use `isolation: "worktree"`** and merge after. No Planning task depends on a Supervisor task or vice versa.

**Console gate (AGENTS.md):** P4 subagent ships code + `make console-test` + scenario text only; the **orchestrator** runs Playwright once, serially, after merge. Read `docs/CONSOLE_TASK_CHECKLIST.md` before P4.

---

## A4. Key Design Decisions

1. **Full-list-replace reducer** for `RuntimeState.plan` (Claude Code `TodoWrite` shape). Simpler than patch ops; the agent always sends the whole list. Concurrent writes are last-write-wins within a super-step (the plan is a self-reminder, not load-bearing).
2. **Inject post-compaction, in the uncached suffix.** The block goes after `compaction_pre_model_hook` returns and after the last cache breakpoint (`apply_cache_markers`, worker `:1404`), so the plan survives compaction **and** a plan *change* invalidates only the minimum cache span. P2 verifies the block sits after the breakpoint, not merely that an *unchanged* plan is byte-identical (the repo cares about cache stability — Track 7 / CLAUDE.md). *(Resolves design open item; review item E9.)*
3. **Markdown checkbox injection format** — human-legible and the Console renders the same shape.
4. **Read-only API**, projected from the checkpoint — no plan table, no mutation endpoint, no migration.
5. **v1 size caps: 50 items, 200-char titles** — enforced by `plan_write`; over-cap → tool returns a validation error (does not silently truncate).
6. **One-`in_progress` is prompt-layer** — the injected preamble asks the agent to keep one item in progress; the tool does not reject multiple.

---

## A5. Risks & Open Questions

| Risk | Disposition |
|---|---|
| Plan injection busts KV-cache on every `plan_write` (planning agents rewrite often) | Inject in the uncached suffix so a change invalidates the minimum span; P2 asserts position relative to the cache breakpoint, not just unchanged-plan byte-identity. |
| Plan grows unbounded | v1 caps (50 items / 200-char titles) enforced by `plan_write`; over-cap returns a validation error. |
| `GET .../plan` called on a supervisor-topology task (no `plan` channel) | Returns `{plan: []}` — documented; not an error. |
| Shared-file collision with the Supervisor track (`state.py`, `_get_tools`, `ActivityPane.tsx`, `types/index.ts`) | Worktree-isolate if both tracks run concurrently (§A3). |

**Genuinely open (none blocking):** whether to later add a patch-op write mode or per-item HITL — both deferred; v1 is full-replace + read-only.

---

## A6. Deployment & Rollout

- **No migration, no new infra.** The plan lives in the existing JSONB checkpoint; the API endpoint and tool ship with a normal deploy.
- **Ships dark via config opt-in:** the Planning Primitive activates only when `plan_write` is in an agent's tool allowlist (the sibling track's `coding` / `investigation` presets seed it). Existing agents are unaffected. Roll back the deploy if a regression appears.

---

## B. Agent Task Files

| Task | File | Description |
|---|---|---|
| P1 | [task-p1-plan-state-and-tool.md](agent_tasks/task-p1-plan-state-and-tool.md) | `RuntimeState.plan` (full-replace reducer) + `plan_write` tool + v1 caps |
| P2 | [task-p2-plan-injection.md](agent_tasks/task-p2-plan-injection.md) | Post-compaction plan injection as a neutral Markdown `SystemMessage`, in the uncached suffix |
| P3 | [task-p3-plan-read-api.md](agent_tasks/task-p3-plan-read-api.md) | `GET /v1/tasks/{id}/plan` read-only projection from the latest checkpoint |
| P4 | [task-p4-console-plan-checklist.md](agent_tasks/task-p4-console-plan-checklist.md) | Console plan checklist + `api.getTaskPlan` fetch hook |
| P5 | [task-p5-planning-integration-tests.md](agent_tasks/task-p5-planning-integration-tests.md) | Plan-survives-compaction + API shape + checklist render; Playwright scenario |
