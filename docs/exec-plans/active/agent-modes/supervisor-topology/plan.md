# Agent Modes — Track: Supervisor Topology (Deep Research)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **One of two tracks under [Agent Modes](../README.md).** Sibling track: [Planning Primitive](../../../completed/agent-modes/planning-primitive/plan.md) (independent; shares only `RuntimeState`, the worker tool registry, and the Console Activity pane — see *Cross-track coordination* in §A3).

**Goal:** Ship the **Supervisor topology** (customer-facing "Deep Research" — Scope → Supervisor → parallel Subagents → Writer, in-process fan-out) together with the small slices the design folds into it: the shared in-process fan-out helper, the `dispatch_subagent` ReAct tool, and presets. Governing design: [Agent Modes design](../../../../design-docs/agent-modes/design.md). The Planning Primitive is a **separate track** (sibling above); the Workflow resource is explicitly **out of scope** (Phase 3).

**Architecture:** Over the existing single-graph worker runtime, `agent_config.topology` (`react` | `supervisor`, **immutable after creation**) selects which LangGraph shape `_build_graph` compiles. A **shared in-process fan-out helper** runs a ReAct sub-agent subgraph with isolated context + tool allowlist and returns a structured summary; it is driven two ways — by the LLM via the `dispatch_subagent` tool (Topology 1) and structurally by the Supervisor graph via LangGraph `Send` (Topology 2). **Pattern A (in-process):** sub-agents run inside the parent's run (one `thread_id`, one task row, one checkpoint stream); there are **no** sub-agent task rows, leases, or `parent_task_id` trees. Per-sub-agent **token+turn ceiling**, **heartbeat**, and **timeout** are enforced **in graph state / the helper**, not via the task/lease layer. Sub-agent activity surfaces as `task_events` sub-steps carrying `iteration` (round) and `subtask` (stable logical id) markers. Presets are named default bundles applied at agent creation.

**Tech Stack:** Python + LangGraph 1.0.5 (`Send`, subgraphs, `durability="sync"`) + LangChain (worker); Spring Boot / Jackson (API config); React 19 / TypeScript / Vite (Console); PostgreSQL (JSONB `agent_config` — no new column; one additive `task_events.event_type` CHECK-constraint migration); Langfuse spans (observability).

---

## A0. Canonical Design Contract & Non-Negotiable Invariants

**Canonical design:** [`docs/design-docs/agent-modes/design.md`](../../../../design-docs/agent-modes/design.md). Every task spec references the relevant section. Read it before implementing any task.

These invariants are **decided** (not open) and any deviation is a plan failure:

1. **Pattern A — in-process fan-out only.** Both `dispatch_subagent` and the Supervisor fan out *inside the parent's run* via LangGraph `Send` / gathered subgraph `ainvoke`. **Do NOT** create sub-agent task rows, a `parent_task_id` column/tree, per-sub-agent leases, a `sub_agent_id` cost-ledger column, or a `waiting_for_subagent` pause state. (Two scouting passes drifted toward this rejected "Pattern B" — it was explicitly dropped 2026-06-03. The upgrade path is documented but **not built here**.)
2. **Topology is immutable after agent creation.** `chat → research` means *create a new agent*, not PATCH. The graph is built once at creation. Other config stays mutable per Track 1/3.
3. **No `agent_config.mode` field.** Customer picks a **preset** → preset sets the internal **`topology`** field → topology fixes the graph. "Deep Research" is the display label of the `research` preset, never a stored enum value.
4. **Findings are immutable and addressable by `finding_id`.** A reduction to fit the Writer's context may drop/reorder/summarize-for-selection but must **never mutate** a finding's `supporting_quote` (ID resolution + the verify pass both break otherwise).
5. **Budget defers wholesale to Track 3.** All sub-agent cost is the parent task's cost — no rollup, no per-tree composition. Over-budget work **pauses** (per-task → manual resume; hourly → auto-recover), never silently fails. The per-sub-agent ceiling bounds *within-super-step* overshoot to `max_fanout × ceiling`; operators size `budget_max_per_task` with that headroom.
6. **`subagent_results` is a checkpointed reducer keyed by `subtask`.** Successful findings *and* failure markers survive a crash idempotently; a re-dispatched subtask updates its own entry, never duplicates.
7. **Depth cap = 2.** Sub-agents are always ReAct (never Supervisor). A Supervisor's structural fan-out consumes one depth level just as a `dispatch_subagent` call does.
8. **Sub-agents are headless — DECIDED (user, 2026-06-05).** Only a **top-level agent** may request user input: the ReAct main agent (via `request_human_input`), or — in the Supervisor topology — the **Scope** node's clarification (a single `interrupt()` at a clean node boundary, before fan-out). A fanned-out **sub-agent may NEVER call `interrupt()`** — its tool allowlist **must exclude** `request_human_input` and any other pause-bearing tool, enforced by filtering in the fan-out helper (S3) and asserted by test (S3/S4/S6). **We deliberately do NOT build LangGraph's multi-interrupt id-keyed resume path** (keep it simple): the worker's scalar `Command(resume=...)` stays as-is, and the exclusion guarantees a fan-out super-step never produces >1 pending interrupt (verified: 2 interrupts → `RuntimeError` in LangGraph 1.0.5; headless sub-agents resume cleanly). See **§A11-E3** (resolved).
9. **Cost attribution is a built mechanism, not automatic.** The cost loop is hardcoded to `event["agent"]`; Supervisor and sub-agent LLM spend will be **silently dropped** from the ledger and budget pause unless S8 extends the loop. This is the single highest-risk integration point. See **§A11-E1**.

---

## A1. Implementation Overview

1. **Config surface (API).** `AgentConfigRequest` gains `topology` (`react` default | `supervisor`), `preset` (string), and a `supervisor` sub-object (`max_fanout_per_iteration`, `max_iterations`, `source_allowlist`, `writer_style`, `scope_clarification_enabled`). Validation bounds + topology-immutability check on `PUT`. Jackson round-trip, canonicalised verbatim, persisted in existing `agent_config` JSONB.
2. **Presets (API).** Platform-owned default bundles (`chat`, `coding`, `investigation`, `research`, `workflow_runner`) applied at agent creation: a preset seeds `topology` + tool allowlist + concurrency/budget defaults + sub-object defaults; explicit request fields override. `research` defaults `max_concurrent_tasks=2` (fan-out pins a slot).
3. **Shared fan-out helper (worker).** `executor/subagents/fanout.py` — runs a ReAct sub-agent subgraph with isolated context window + tool allowlist, enforcing in graph state a per-sub-agent **token+turn ceiling**, emitting a per-sub-agent **heartbeat** event, enforcing a per-sub-agent **timeout**, incrementing a `depth` counter (cap 2), and returning a structured summary. Built once; used by both drivers.
4. **`dispatch_subagent` tool (worker).** A built-in tool (Topology 1) wrapping the helper; the LLM emits the call, the result returns as a `ToolMessage`. Gated by allowlist; `budget` arg = per-sub-agent ceiling.
5. **Supervisor graph — Scope.** Clarity assessment; conditional clarification via `interrupt()` (reuses `waiting_for_input`); produces the immutable **brief** ("north star").
6. **Supervisor graph — Supervisor node + structural fan-out + iteration.** Supervisor emits a parsed `subtasks: [...]` contract; the graph `Send`s N sub-agents in parallel via the helper; results land in the `subagent_results` reducer keyed by `subtask`; Supervisor decides "need more?" up to `max_iterations`; partial-failure handling (fail only if *zero* returned).
7. **Supervisor graph — Subagents + Writer + citation binding.** Subagents emit structured `{finding_id, claim, source_url, supporting_quote}`; one-shot Writer cites by `finding_id` only; a thin verification pass confirms each cited quote supports its sentence; runtime resolves IDs → citations at render time.
8. **Graph-build branching + cost-attribution mechanism (worker).** `_build_graph` selects ReAct vs. Supervisor on `agent_config.topology`; Supervisor graph compiled with `durability="sync"`. **⚠ Cost attribution is a built mechanism, not an audit** — the existing cost loop (`graph.py:3166`) is gated on `event["agent"]`, and Supervisor/sub-agent nodes emit under *other* node keys, so without new code a Deep Research run records ~$0 and never trips the budget pause. S8 must extend the cost-recording loop to capture `usage_metadata` from every LLM-bearing Supervisor node (and sub-agent fan-out), attribute it to the parent's super-step `checkpoint_id` via an **additive** ledger write, and prove it with a wide-fan-out test that asserts a **non-zero** ledger delta. See **§A11-E1**. Budget pause must be evaluated at the **fan-out super-step boundary**, not mid-branch (§A11-E2).
9. **Observability (DB + API + worker).** New `task_events` event types for sub-agent lifecycle (`subagent_started`, `subagent_finding`, `subagent_failed`, `supervisor_iteration`) carrying `iteration` + `subtask` in `details`; `ActivityProjectionService` maps them into an expandable round→sub-agent tree. **Scope note:** sub-agents run in *isolated context windows* not threaded into the parent's `messages` channel, so the projection (which reads `messages`) surfaces the marker **skeleton** (rounds, sub-agent starts, findings, failures) but **not** sub-agent turn-by-turn reasoning — full sub-agent transcripts live in Langfuse spans. The Console tree expands to marker detail, not a full sub-agent conversation, unless §A11-E5 is resolved to persist a distilled transcript. One additive migration extends the CHECK constraint.
10. **Console (Supervisor).** Preset selector at creation (locked on edit); a "Deep Research" config section; sub-agent activity rendered as an expandable tree in the Activity pane.
11. **Integration + E2E + browser tests (Supervisor).** Fan-out determinism, partial-failure, citation-binding, crash-resume-forward, redrive-recompute; Playwright scenario for the preset flow + sub-agent tree.

---

## A2. Impacted Components

| Component | Path | Change | Stream |
|---|---|---|---|
| Agent config (API) | `services/api-service/.../model/request/AgentConfigRequest.java`, new `SupervisorConfigRequest.java`, `service/ConfigValidationHelper.java`, `service/AgentService.java`, `config/ValidationConstants.java` | new + mod | S |
| Presets (API) | new `service/PresetDefaults.java` (or `config/PresetDefaults.java`), `service/AgentService.java` (canonicalize) | new + mod | S |
| Topology immutability (API) | `service/AgentService.java` (`updateAgent`) | mod | S |
| Shared fan-out helper | `services/worker-service/executor/subagents/fanout.py`, `executor/subagents/__init__.py` | new | S |
| `dispatch_subagent` tool | `services/worker-service/tools/definitions.py` (or new `tools/subagent_tools.py`), `executor/graph.py` (`_get_tools`) | new + mod | S |
| Supervisor graph | `services/worker-service/executor/supervisor/graph.py`, `supervisor/nodes.py`, `supervisor/prompts.py`, `supervisor/state.py`, `supervisor/citations.py` | new | S |
| Graph-build branching | `services/worker-service/executor/graph.py` (`_build_graph`, `execute_task`) | mod | S |
| Sub-agent state fields | `services/worker-service/executor/compaction/state.py` (or `supervisor/state.py` superset) | mod | S |
| Budget carve-out audit | `services/worker-service/executor/graph.py` (astream cost loop ~3160) | mod | S |
| task_events event types | `infrastructure/database/migrations/0025_agent_modes_subagent_events.sql`, API `service/TaskEventService.java` callers, worker `_insert_task_event` callers | new + mod | S |
| Activity projection | `services/api-service/.../service/ActivityProjectionService.java`, `model/response/ActivityEventResponse.java` | mod | S |
| Console — preset + supervisor config | `services/console/src/features/agents/CreateAgentDialog.tsx`, `AgentDetailPage.tsx`, new `features/agents/SupervisorConfigSection.tsx` | new + mod | S |
| Console — sub-agent tree | `services/console/src/features/task-detail/ActivityPane.tsx`, `types/index.ts` | mod | S |
| Tests | worker `tests/`, `tests/backend-integration/`, Console unit + `CONSOLE_BROWSER_TESTING.md` scenarios | new | S |

**Verified anchors (from codebase scout — cite these in tasks):** `_build_graph` `services/worker-service/executor/graph.py:1059`; `agent_node` `:1344`; `StateGraph(state_type)` `:1555`; tool registration `_get_tools` `:850-1057`, `llm.bind_tools` `:1269`, `MAX_TOOLS_PER_AGENT`; compaction hook call `:1383`; cache markers `:1404`; `astream(... durability="sync")` `:3127`; budget carve-out skip `:3160-3163`; `_check_budget_and_pause` `:3796`; `execute_task` `:2611`, `agent_config_snapshot` load `:2615`; `RuntimeState` + reducers `executor/compaction/state.py`; `compaction_pre_model_hook` `executor/compaction/pre_model_hook.py`; `interrupt()` tool `tools/definitions.py:449`; cost ledger `core/cost_ledger_repository.py` (`insert_cost_row`, `sum_task_cost`, `sum_hourly_cost_for_agent`); `_insert_task_event` `core/reaper.py:523`; `task_events` schema `infrastructure/database/migrations/0006_runtime_state_model.sql:25`, CHECK extended in `0020`/`0024` (latest migration **0024**, next is **0025**); budget config `0007_scheduler_and_budgets.sql` (`max_concurrent_tasks` default 5); heartbeat `core/heartbeat.py`; poller/lease `core/poller.py`. API: `AgentConfigRequest.java`, `ConfigValidationHelper.validateAgentConfig` `:314`, `AgentService.createAgent` `:50` / `updateAgent` `:130` / `canonicalizeConfig` `:168`; `TaskController` events `:162` / activity `:190`; `ActivityProjectionService`; `agent_config JSONB` `0005_agents_table.sql:9`. Console: `CreateAgentDialog.tsx`, `AgentDetailPage.tsx`, `ContextManagementSection.tsx` (section pattern), `ActivityPane.tsx`, `api/client.ts` `listActivity` `:249`, `types/index.ts` `ActivityEvent`.

---

## A3. Dependency Graph

```
  S1 (API config: topology/preset/supervisor sub-object + immutability) ─┬─► S2 (Presets apply) ──┐
                                                                          │                         │
  S3 (Shared fan-out helper) ──┬──► S4 (dispatch_subagent tool) ─────────────────────────┐         │
                               │                                                          │         │
                               └──► S5 (Scope) ─► S6 (Supervisor + Send fan-out + iter) ─► S7 (Subagents+Writer+citations)
                                                                                          │
                          S3..S7 ──► S8 (graph-build branching + cost mechanism) ─────────┤
                                                                                          │
  S9 (task_events types + migration 0025 + ActivityProjection) ───────────────────────────┤
                                                                                          │
  S1 ──► S10 (Console: preset selector + supervisor section + sub-agent tree) ────────────┤
                                                                                          │
                          S1..S10 ──► S11 (Integration + E2E + browser, Supervisor)
```

**Cross-track coordination (with the [Planning Primitive](../../../completed/agent-modes/planning-primitive/plan.md) track):** the two tracks are independent but touch four shared files — `executor/compaction/state.py` (both add a `RuntimeState` field), `executor/graph.py::_get_tools` (both register a built-in tool), `services/console/src/features/task-detail/ActivityPane.tsx`, and `types/index.ts`. **If both tracks run concurrently, any agent editing one of these MUST use `isolation: "worktree"`** and merge after. Cleanest sequencing: land one track's `RuntimeState`/`_get_tools` change, rebase the other. None of the Supervisor tasks *depend* on a Planning task or vice versa.

**Parallelisation & worktree safety within this track (AGENTS.md §Parallel Subagent Safety):**

- **S1 (Java API) ∥ S3 (Python worker)** — zero file overlap, safe in parallel.
- **S5/S6/S7** all add files under `executor/supervisor/` — serialize S5→S6→S7 (they build on each other's node contracts) or worktree-isolate.
- **S3 is the hard blocker** for S4, S5–S7 (the helper is the shared primitive). Land it green first.
- **S8 edits `executor/graph.py` heavily** (`_build_graph` branching) — any parallel agent touching `graph.py` uses worktree isolation.
- **S9 (migration + Java projection) ∥ worker tasks** — different area; the migration must reach prod before the worker emits the new event types (deploy-order constraint, see §A6).
- **S10 / Console** owns `make start`/Playwright collisions — **subagents ship code + unit tests + scenario text only; the orchestrator runs Playwright serially after merge** (AGENTS.md §Browser verification is the orchestrator's job).

---

## A4. Data / API / Schema Changes

**No new tables. No new columns on `agents` or `tasks`.** `agent_config` is JSONB (`0005_agents_table.sql:9`) — `topology`, `preset`, and `supervisor` sub-object are additive JSONB keys. Sub-agent state (`subagent_results`, `iteration`, `subtask`, brief, findings) lives in LangGraph checkpoint blobs (JSONB, already schema-compatible).

**One additive migration — `0025_agent_modes_subagent_events.sql`:** extends the `task_events.event_type` CHECK constraint (DROP + re-ADD pattern, per `0024`/`0020`) with: `subagent_started`, `subagent_finding`, `subagent_failed`, `supervisor_iteration`. Additive + non-breaking for existing rows. `iteration` (int) and `subtask` (string) ride in the existing `details` JSONB — **no schema column for them**.

**API surface:**
- `POST /v1/agents` / `PUT /v1/agents/{id}` accept `topology`, `preset`, `supervisor`. `PUT` rejects a `topology` change with 400 (immutability). Validation bounds: `max_fanout_per_iteration ∈ [1, 20]`, `max_iterations ∈ [1, 10]`, `source_allowlist ≤ 50` entries (matches `exclude_tools`/`tool_servers` cap), `writer_style ∈ {formal_report, annotated_bullets}`, `scope_clarification_enabled` boolean. `topology ∈ {react, supervisor}`; absent → `react`.
- **No task-submission payload change.** A task targets `agent_id`; the Supervisor shape is fixed by the agent's topology. (`workflow_id` target is Phase-3, not widened here.) The `GET /v1/tasks/{id}/plan` read endpoint belongs to the [Planning Primitive](../../../completed/agent-modes/planning-primitive/plan.md) track, not this one.

**Cost ledger:** unchanged schema, but a **new attribution path** (S8) is required. The existing cost loop only records spend from the `event["agent"]` node; Supervisor/sub-agent nodes emit under other keys and would be dropped. S8 adds an **additive** `model_token_spend` ledger write at the parent's super-step `checkpoint_id` aggregating every LLM-bearing Supervisor node's `usage_metadata` (Pattern A — still no `sub_agent_id` column, no per-sub-agent rows). The `compaction.tier3` partial-unique index is *not* in play (it's scoped to `operation='compaction.tier3'`), so wide fan-out poses no ledger-idempotency hazard — but the per-checkpoint `checkpoints.cost_microdollars` write must be **additive** (`add_cost_and_preserve_metadata`), never overwrite. See **§A11-E1**.

---

## A4.1. Task Handoff Outputs (canonical contracts — names are load-bearing)

| Task | Output contract |
|---|---|
| S1 | `agent_config.{topology, preset, supervisor}` accepted/validated/canonicalised; `topology` immutable on `PUT` (400 on change); bounds enforced; absent topology defaults `react`; requests round-trip verbatim. |
| S2 | `PresetDefaults` maps preset name → seeded defaults (topology, tools, concurrency, budgets, sub-object defaults); explicit fields override; `research` seeds `max_concurrent_tasks=2`, `topology=supervisor`, web tool allowlist, fan-out width 5. Unknown preset → 400. |
| S3 | `executor/subagents/fanout.py` exposes the sub-agent as a **compiled ReAct subgraph node** (isolated context window via a *separate* internal message channel; own tool allowlist) that callers reach **via `Send`** and that **shares the parent checkpointer** (namespaced sub-checkpoint → per-inner-turn crash resume + persisted transcript; spike #2/#3). **Not** an imperative `ainvoke`. The run-helper enforces token+turn `ceiling` in state; **filters the passed `tools` to exclude any `interrupt()`-bearing tool (`request_human_input`) — sub-agents are headless (§A0 inv. 8)**; emits a `subagent.heartbeat` event to the **Langfuse-span sink** (decided sink: a span event — NOT a `task_events` row, NOT a `tasks.lease_expiry` touch; §A11-E4); enforces `timeout`; returns a structured failure marker on exhaustion (never raises into the graph); rejects `depth > MAX_SUBAGENT_DEPTH (=2)`. Pure-ish, unit-tested with a fake model. |
| S4 | `dispatch_subagent(prompt, tools, budget)` built-in tool. The LLM emits the call; a **post-agent routing edge intercepts it and `Send`s it to the shared subagent node** (S3) — **not** executed inside the ToolNode — so it gets the same per-inner-turn durability as the Supervisor. The subagent node threads its summary back as a `ToolMessage` keyed to the original `tool_call_id` (inner messages stay on the separate channel). Allowlist-gated; `depth`/`budget` (= ceiling) from graph state. Mixed turns (a `dispatch_subagent` call alongside normal tool calls) split: dispatch → `Send`, others → ToolNode; every `tool_call_id` gets exactly one `ToolMessage` before the next LLM call. |
| S5 | `supervisor/nodes.py::scope_node` — clarity assessment; conditional `interrupt()` clarify (reuses `waiting_for_input`); emits immutable `brief` into supervisor state. |
| S6 | `supervisor/nodes.py::supervisor_node` emits parsed `subtasks: [{subtask, prompt}]`; graph `Send`s them through `run_subagent`; results merge into `subagent_results` reducer keyed by `subtask`; iteration loop bounded by `max_iterations` + `max_fanout_per_iteration`; partial-failure → proceed unless *zero* returned. **`subtask` ids are minted deterministically by S6 as `f"{iteration}.{index}"` (new) / carried-forward only on explicit re-dispatch — never trusted from the LLM**, so two same-round subtasks cannot collide in the reducer and lose work (§A11-E8). The Supervisor `Send` passes `depth=1` for the first fan-out level (Supervisor itself is depth 0). |
| S7 | `supervisor/nodes.py::{subagent_node, writer_node}` + `supervisor/citations.py` — subagents emit `{finding_id, claim, source_url, supporting_quote}` (immutable quote); Writer cites by `finding_id`; verify pass flags unsupported citations; IDs resolved → citations at render. |
| S8 | `_build_graph` branches on `agent_config.get("topology","react")`; Supervisor graph compiled `durability="sync"`. **Cost-attribution mechanism** (not audit): extend the `graph.py:3166` cost loop to capture `usage_metadata` from every LLM-bearing Supervisor node + fan-out, additively attribute to the parent super-step `checkpoint_id`; **budget pause evaluated at the fan-out super-step boundary** (not mid-branch — a mid-`Send` `return` abandons live sibling branches). Test asserts a **non-zero ledger delta** from a wide fan-out and that the pause fires at the boundary. (§A11-E1, §A11-E2.) |
| S9 | Migration `0025` extends `task_events` CHECK; worker emits `subagent_started/finding/failed`, `supervisor_iteration` with `{iteration, subtask}` in details; `ActivityProjectionService` projects them into `marker.subagent.*` / tree-groupable events. |
| S10 | Console: preset selector on create (read-only on detail), `SupervisorConfigSection` fields with `data-testid`s, sub-agent expandable tree in Activity pane. Browser-verified by orchestrator. |
| S11 | Supervisor E2E manifest: fan-out determinism, partial-failure, citation binding, resume-forward vs. redrive, caps; Playwright scenario. |

---

## A5. Integration Points

| Caller | Callee | Interface | Failure handling |
|---|---|---|---|
| `AgentController` | `ConfigValidationHelper.validateSupervisorConfig` / `validatePreset` | bounds + enum + immutability | 400 per-field, Track-5/7 message style |
| `AgentService.updateAgent` | current-row topology compare | reject topology change | 400 "topology is immutable after agent creation" |
| `_build_graph` | topology switch | build ReAct or Supervisor graph | unknown topology → fail task build (defensive; API already validated) |
| `dispatch_subagent` tool / Supervisor `Send` | `run_subagent` (helper) | isolated subgraph + ceiling + heartbeat + timeout | ceiling/timeout exhaustion → structured failure marker, not graph error |
| Supervisor graph | `subagent_results` reducer | keyed by `subtask`, checkpointed | crash → resume-forward restores completed siblings; only unfinished re-run |
| Writer | `citations.resolve` / `citations.verify` | `finding_id` → citation; quote-supports-sentence | unresolved id → render error flag; unsupported quote → verify flag (not fabricated source) |
| worker fan-out | `_insert_task_event` | `subagent_*` / `supervisor_iteration` + `{iteration, subtask}` details | **at-least-once, NOT atomic with checkpoint writes** (the checkpointer owns its own connections/txns — `checkpointer/postgres.py:181-204`, `:247-260`); per-turn resume re-emits → duplicates expected; `(event_type, iteration, subtask)` is the dedup key; projection is duplicate-tolerant |
| `ActivityProjectionService` | new `task_events` types | map to tree-groupable markers | unknown type tolerated (forward-compat) |
| Console | activity sub-agent tree | render pre-shaped markers | empty/absent → render nothing, no error |

---

## A6. Deployment & Rollout

1. **Migration `0025_agent_modes_subagent_events.sql`** (CHECK-constraint DROP + re-ADD, per `0024`) **must land in production before** any worker build that emits `subagent_*` / `supervisor_iteration` events — otherwise the `INSERT INTO task_events` violates the CHECK. Merge S9's migration commit before S8/S6's emitting code ships to prod. CI auto-applies migrations via the `[0-9][0-9][0-9][0-9]_*.sql` glob (no CI wiring needed — confirm in `.github/workflows/ci.yml`).
2. **No new service containers / infra deps** — no CI service-container additions required.
3. **Gradual exposure is config-driven, not a deploy flag.** Supervisor topology only runs for agents created with `topology=supervisor` (i.e. the `research` preset). Existing agents are unaffected (absent topology → `react`). It ships "dark" until a customer opts in via config — no env-var escape hatch needed; roll back the deploy if a regression appears.
4. **Worker-pool sizing note (ops) — two distinct limits, do not conflate (§A11-E6).** There are *two* `max_concurrent_tasks`: (a) `agents.max_concurrent_tasks` (DB column, default 5) — per-agent admission, what the `research` preset sets to 2; and (b) `config.max_concurrent_tasks` (worker env, default 10) — the per-**worker-process** `asyncio.Semaphore` (`core/poller.py:129`). The preset's per-agent=2 does **not** bound cross-tenant starvation: a handful of multi-minute fan-outs from *different* agents can saturate one worker's process-wide semaphore and starve every other tenant's quick tasks on that worker. The real mitigation is **worker-pool isolation / sizing** (b), not the preset value (a). **Decided (v1): ops guidance, no code gate** — run `supervisor`-topology agents on a worker pool sized for long fan-outs, separate from latency-sensitive `chat` traffic. A per-worker cap on concurrent `supervisor` tasks is the documented upgrade **iff** multi-tenant pressure appears; not built for v1 (§A11-E6, resolved).
5. **Task-timeout — DECIDED (§A11-E7).** A whole Deep Research run is *one task*; the reaper dead-letters any task where `timeout_reference_at + task_timeout_seconds < NOW()` (`core/reaper.py:98`), and `timeout_reference_at` is set **once at creation** (default `task_timeout_seconds=3600`), independent of the (healthy) lease heartbeat — so a wide, multi-iteration fan-out exceeding 3600s would be **dead-lettered mid-run**. **Resolved: the `research` preset seeds `task_timeout_seconds = 14400` (4 h)** (S2), seeded as an `agent_config` default + `TaskService` submission-time fallback (no agent-level column today, `TaskService.java:135`). Sized from `max_iterations`×`max_fanout`×per-sub-agent-ceiling; **tunable**. Documented upgrade if 4 h proves tight: reset `timeout_reference_at` at super-step boundaries (a no-progress detector rather than a wall-clock cap) — deferred, not v1.

---

## A7. Observability

**New structured events / Langfuse spans:**
- `subagent_started` — `{iteration, subtask, prompt_preview, tool_allowlist, depth}`
- `subagent_finding` — `{iteration, subtask, finding_id, source_url}` (claim/quote in span, not the row, to bound size)
- `subagent_failed` — `{iteration, subtask, reason: ceiling|timeout|error}`
- `supervisor_iteration` — `{iteration, subtasks_emitted, decision: continue|stop, reason}`
- Langfuse: one span per sub-agent (`subagent.run`, attrs `subtask`, `tokens`, `turns`, `outcome`), one per Supervisor iteration, one per Writer + verify pass.
- `subagent.heartbeat` — emitted by the helper while awaiting (the parent lease stays healthy; silence ≠ liveness).

**Caps surfaced (no silent truncation):** when the Writer's finding corpus is reduced (open algorithm — see design *Open decisions*), `log()` what was dropped; when a fan-out hits `max_fanout_per_iteration` or `max_iterations`, emit `supervisor_iteration` with the cap reason.

---

## A8. Risks & Open Questions

| Risk / open item | Disposition |
|---|---|
| Implementer rebuilds Pattern B (sub-agent task rows/leases) | **Blocked by A0 invariant 1.** S3/S4/S6/S8 task specs restate it; review gate checks for `parent_task_id`/`sub_agent_id`/`waiting_for_subagent`. |
| `Send` + Postgres checkpointer cost-ledger dedup (scout flagged friction) | Resolved by Pattern A: a fan-out is **one super-step → one `checkpoint_id`**; sub-agent token spend rolls into the parent `agent` operation. No per-sub-agent ledger rows, so no dedup-cardinality problem. S8 asserts this with a wide-fan-out cost test. |
| Wide-fan-out checkpoint payload size (all sub-agent state in one super-step write) | **Open (design Open decisions, Supervisor track).** v1: emit a `checkpoint.oversized` warning past a threshold; cap/offload deferred. S6 logs payload bytes. |
| One-shot Writer context overflow (many findings) | **Open algorithm (design Open decisions).** v1: hard cap on findings into the Writer + `log()` dropped; immutability invariant preserved (select/reorder only). S7 owns the v1 reduction + the open flag. |
| Scope clarification deadlocks headless customers | `scope_clarification_enabled=false` → Scope never `interrupt()`s, proceeds on best-effort brief. S5 honors the flag. |
| Per-sub-agent ceiling too low → sub-agents truncate mid-thought | Ceiling default sized from the `research` preset; surfaced as `subagent_failed{reason:ceiling}` so operators can tune. Not silent. |
| Topology-immutability check misses a nested edit | S1 compares canonicalised `topology` value specifically; unit test PATCHes `react→supervisor` and asserts 400. |
| Sub-agent heartbeat vs. parent heartbeat confusion | Helper emits an **event** (`subagent.heartbeat`), it does **not** touch the `tasks.lease_expiry` row (that stays the parent's single heartbeat per `core/heartbeat.py`). S3 spec is explicit. |

**Genuinely open (carried from design, each owned by a task):** ~~Writer reduction algorithm (S7), checkpoint payload size cap (S6), finer in-flight metering (S8 notes, deferred).~~ **All three CLOSED at S11 acceptance (2026-06-09) — see the §A12 Deferred Decisions Ledger dispositions (D2/D1/D3 respectively, each Closed with the measured metric as evidence).** No genuinely-open design items remain.

---

## A9. Orchestrator Guidance

- **Read `docs/design-docs/agent-modes/design.md` first.** Each task names its governing section. The A0 invariants are non-negotiable.
- **Pattern A discipline:** no sub-agent task rows, no `parent_task_id`, no `sub_agent_id` ledger column, no `waiting_for_subagent`. Fan-out is in-graph. If a task seems to need cross-task spawn, stop — it doesn't (that's the deferred Pattern B upgrade).
- **Build the shared helper (S3) once.** `dispatch_subagent` and the Supervisor are two *drivers* over the same `run_subagent`. Don't fork the machinery.
- **`durability="sync"`** for the Supervisor graph compile/invoke, matching `executor/graph.py:3127`. Do not switch to LangGraph's `"async"` default.
- **Findings immutability:** the reduction step may select/reorder/summarize-for-selection but must never rewrite `supporting_quote`.
- **Budget:** never add a refund path; cost is cumulative (design *Budget and redrive*). Carve-out only what Track 3 already carves; do not invent new exemptions.
- **Worktree safety:** `executor/compaction/state.py`, `executor/graph.py::_get_tools`, `ActivityPane.tsx`, `types/index.ts` are shared with the [Planning Primitive](../../../completed/agent-modes/planning-primitive/plan.md) track — if both run concurrently, worktree-isolate parallel edits and merge after (see *Cross-track coordination* in §A3).
- **Console gate:** subagents ship code + `make console-test` + scenario text only; the **orchestrator** runs Playwright once, serially, after merge (AGENTS.md). Read `docs/CONSOLE_TASK_CHECKLIST.md` before any Console task; update the agent-config coverage matrix in the same commit (add an `agent_mode` / `supervisor` row; `topology`/`preset` rendered on >1 surface → Template D parity assertions).
- **Tests are worktree-concurrency-safe:** any test binding a port uses an ephemeral port (`scripts/e2e/free-port.py` / `:0`); never raw `pytest tests/backend-integration` in a worktree — use `make e2e-test PYTEST_ARGS='-k ...'`.
- **Do NOT build (this plan):** Workflow resource / `execute_workflow` / direct `workflow_id` submission (Phase 3); Pattern B durable cross-task sub-agents; Plan-and-Execute / Reflexion / LLMCompiler topologies (design *Patterns not built*); any `agent_config.mode` field.

---

## A10. Key Design Decisions (carried from design.md, made concrete here)

1. **Topology selects the graph at build time; immutable after creation.** `_build_graph` branches on `agent_config.topology`; `PUT` rejects topology changes.
2. **Preset → topology, not a `mode` field.** Presets are API-layer default bundles; the only stored shape selector is `topology`.
3. **One shared subagent subgraph node, two drivers — both route via `Send`.** `dispatch_subagent` (a post-agent routing edge intercepts the LLM's tool call and `Send`s it) and the Supervisor (structural `Send` from its subtask list) both fan out through the *same* checkpointed subagent node. Differ only in who triggers the `Send` and how the result is injected (`ToolMessage` vs. `subagent_results`).
4. **Pattern A in-process fan-out, with per-inner-turn durability.** One task, one `thread_id`, one checkpoint stream; sub-agent activity = events, not rows. The sub-agent is a **checkpointed subgraph node** (shares the parent checkpointer, namespaced) — so a crash mid-sub-agent resumes at the inner turn it died on (spike-verified), and the sub-agent transcript is persisted in its sub-checkpoint (resolves E5). The "gathered `ainvoke`" wiring is rejected (forfeits per-turn durability).
5. **Per-sub-agent ceiling + heartbeat + timeout in graph state/helper.** Bounds within-super-step overshoot, liveness, and runaway branches — *not* the task/lease layer.
6. **`subagent_results` checkpointed reducer keyed by `subtask`.** Idempotent partial results; resume-forward vs. redrive distinction preserved.
7. **Citation binding by `finding_id` + immutable quotes + verify pass.** No fabricated sources; one-shot Writer preserved.
8. **Depth cap 2; sub-agents are ReAct-only.**
9. **Budget defers to Track 3; cumulative, pause-not-fail, no refund.**
10. **No new tables/columns; one additive `task_events` CHECK migration (`0025`).**
11. **Ships dark via config opt-in (the `research` preset / `topology=supervisor`), not a deploy flag.**

---

## B. Agent Task Files

| Task | File | Description | Stream |
|---|---|---|---|
| S1 | [task-s1-api-topology-preset-config.md](agent_tasks/task-s1-api-topology-preset-config.md) | API: `topology` (immutable) + `preset` + `supervisor` sub-object; validation; canonicalisation | S |
| S2 | [task-s2-presets.md](agent_tasks/task-s2-presets.md) | `PresetDefaults` bundles applied at creation; `research` low-concurrency default | S |
| S3 | [task-s3-shared-fanout-helper.md](agent_tasks/task-s3-shared-fanout-helper.md) | `run_subagent` isolated-context subgraph + ceiling + heartbeat + timeout + depth cap | S |
| S4 | [task-s4-dispatch-subagent-tool.md](agent_tasks/task-s4-dispatch-subagent-tool.md) | `dispatch_subagent` ReAct tool (Topology 1) wrapping the helper | S |
| S5 | [task-s5-supervisor-scope.md](agent_tasks/task-s5-supervisor-scope.md) | Scope node: clarity assessment + conditional clarify + immutable brief | S |
| S6 | [task-s6-supervisor-fanout-iteration.md](agent_tasks/task-s6-supervisor-fanout-iteration.md) | Supervisor node + structural `Send` fan-out + iteration loop + partial-failure + `subagent_results` reducer | S |
| S7 | [task-s7-subagents-writer-citations.md](agent_tasks/task-s7-subagents-writer-citations.md) | Subagent findings contract + one-shot Writer + citation binding + verify pass | S |
| S8 | [task-s8-graph-build-branching.md](agent_tasks/task-s8-graph-build-branching.md) | `_build_graph` topology branch + `durability="sync"` + **cost-attribution mechanism** + super-step-boundary budget pause (§A11-E1/E2) | S |
| S9 | [task-s9-observability-events.md](agent_tasks/task-s9-observability-events.md) | Migration `0025` + worker event emission + `ActivityProjectionService` tree mapping | S |
| S10 | [task-s10-console-supervisor.md](agent_tasks/task-s10-console-supervisor.md) | Preset selector (locked on edit) + supervisor config section + sub-agent tree | S |
| S11 | [task-s11-supervisor-integration-tests.md](agent_tasks/task-s11-supervisor-integration-tests.md) | Supervisor E2E + Playwright scenario | S |

> Planning Primitive tasks (P1–P5) live in the sibling [Planning Primitive track](../../../completed/agent-modes/planning-primitive/plan.md).

---

## A11. Design Escalations Surfaced by Review (resolve before/at execution)

A four-lens review (design-fidelity, codebase-integration, completeness, adversarial) ran against this plan on 2026-06-05. Two reviewers **read the live code and reproduced behavior in LangGraph 1.0.5**. The plan was design-faithful and well-grounded (every spot-checked anchor was accurate), but the review found several elements the design doc treated as settled that rested on assumptions the codebase contradicts.

**Status (2026-06-05): all ten escalations are now resolved or decided** — via six throwaway spikes (E1, E2, E3, E5, and the per-turn/cross-process durability that underpins them) and concrete decisions (E6, E7, E8, E9, E10, E4). None remains a blocker. Each resolution is wired into the task contracts above; the rows below record the disposition + evidence. What stays genuinely *open* is not on this list — it's the three build-time design items each owned by a task (Writer reduction algorithm → S7; wide-fan-out checkpoint-size cap → S6; finer in-flight metering → S8 note), listed under §A8.

| ID | Severity | Escalation | Decision needed | Owning task(s) | Evidence (verified) |
|---|---|---|---|---|---|
| **E1** | **✅ MECHANISM PROVEN** (spike #6) | **Cost loop is `event["agent"]`-only.** Supervisor/sub-agent nodes emit under other node keys → without a fix, Deep Research records ~$0 and never pauses. | **Resolved: S8 streams with `subgraphs=True`, aggregates `usage_metadata` across namespaces, and writes it additively to the parent super-step `checkpoint_id`.** Spike #6 captured all 6 sub-agent LLM calls + exact totals; baseline captured 0. S11 asserts a non-zero ledger delta. | S8 (build the mechanism), S11 (non-zero-delta test) | spike #6; `graph.py:3166`, node `"agent"` `:1556`; ledger append-only (`cost_ledger_repository.py:32-43`). |
| **E2** | **✅ RESOLVED** (spikes #1/#5) | **Budget pause mid-fan-out abandons live sibling branches.** A bare `return` mid-`Send`-super-step strands in-flight branches. | **Resolved: evaluate the budget pause at the fan-out super-step *boundary*, never mid-branch.** Spikes confirm the boundary path is durable — completed branches restored, crashed branch resumes per-turn (even cross-process on Postgres, spike #5). S6 checkpoints between iterations; S8 pauses at the boundary; S11 tests it. | S6, S8; test in S11 | spikes #1 (sibling preserved) + #5 (Postgres cross-process resume); `durability="sync"` `:3127`. |
| **E3** | **✅ DECIDED** (user, 2026-06-05) | **`interrupt()` inside fan-out is unresumable.** ≥2 sub-agents interrupting in one `Send` super-step → multiple pending interrupts → the worker's scalar `Command(resume=...)` raises `RuntimeError`. | **Resolved: sub-agents are headless (§A0 inv. 8).** Only top-level agents (ReAct main / Scope) request input. Helper filters out `interrupt()`-bearing tools. **Multi-interrupt resume is explicitly NOT built** (keep it simple). | S3 (filter + test), S4/S6 (assert) | Reproduced in LangGraph 1.0.5; headless sub-agents resume cleanly (spike). worker resume `graph.py:3108-3112`, detect `:3283-3308`; `request_human_input` `tools/definitions.py:449`. |
| **E4** | **✅ DECIDED** | **`subagent.heartbeat` sink undefined.** No task said where it lands; risk of a silent no-op callable, dropping the design's only wedged-branch detector. | Confirm the decided sink: a **Langfuse span event** (not a `task_events` row, not a lease touch). Add an S11 assertion that a long sub-agent emits ≥1 heartbeat. | S3 (emit), S11 (assert) | Design lines 196/213 make heartbeat load-bearing; S9 excludes it from `task_events`. Design + completeness reviewers. |
| **E5** | **✅ RESOLVED** (spike, 2026-06-05) | **Sub-agent reasoning invisible to Activity projection.** `ActivityProjectionService` reads the parent `messages` channel; the isolated sub-agent state isn't *there*. | **Resolved by the persistence model:** because each sub-agent is a checkpointed subgraph node (S3), its full transcript already lives in its **namespaced sub-checkpoint** — no new table/store. Console drill-in reads that namespace on demand. v1 still shows the `task_events` marker **skeleton** by default (S9); the **full transcript on click** is a *read path* into the sub-checkpoint (S9/S10), not new persistence. | S9 (read path), S10 (drill-in UI) | Sub-agent transcript persisted in sub-checkpoint (spike #2/#3); `ActivityProjectionService` reads only top-level `messages` today (`:100-118`) → needs a namespace-descending read. |
| **E6** | **✅ RESOLVED (ops guidance)** | **Two `max_concurrent_tasks` conflated.** Preset=2 sets the per-agent DB column; the binding constraint is the per-worker-process semaphore — cross-tenant starvation unmitigated by the preset alone. | **Resolved v1: ops guidance, no code gate.** `agents.max_concurrent_tasks=2` (preset) bounds per-agent admission; cross-tenant isolation is a **worker-pool sizing** decision — run `supervisor`-topology agents on a pool sized for long fan-outs, separate from latency-sensitive `chat` traffic (§A6.4). A per-worker cap on concurrent `supervisor` tasks is the documented upgrade **iff** multi-tenant pressure appears — not built for v1. | ops/rollout (doc only) | `agents.max_concurrent_tasks` `poller.py:49` vs `config.max_concurrent_tasks` semaphore `poller.py:129`, `config.py:60`. |
| **E7** | **✅ DECIDED** | **`task_timeout_seconds` dead-letters long fan-outs.** One-task runs exceeding the 3600s default are reaper-dead-lettered (not paused), stranding checkpoint state. | **Decided: the `research` preset seeds a higher default `task_timeout_seconds` = `14400` (4 h)** — seeded as an `agent_config` default with a `TaskService` submission-time fallback (no agent-level column exists today; `TaskService.java:135`). Sized from `max_iterations`×`max_fanout`×per-sub-agent-ceiling wall-clock; **tunable**. Documented upgrade if even 4 h is tight: reset `timeout_reference_at` at super-step boundaries (turns the cap into a no-progress detector) — deferred. | S2 (preset default + fallback) | `reaper.py:98`; `timeout_reference_at` set once (migration `0004`); defaulted at submission `TaskService.java:135`; heartbeat separate (`heartbeat.py:131`). |
| **E8** | **✅ DECIDED** | **Within-iteration `subtask`-id collision loses work.** Reducer keyed by `subtask` is idempotent cross-round, but two same-round subtasks with a colliding (LLM-chosen) id silently overwrite → lost findings + burned tokens. | Ratify deterministic id minting in S6 (`f"{iteration}.{index}"`; carry-forward only on explicit re-dispatch). Add a within-iteration collision test. | S6 | S6 tested cross-round only. Adversarial reviewer. (Now in §A4.1 S6 row.) |
| **E10** | **✅ DECIDED** | **Migration 0025 CHECK lock.** DROP + re-ADD CHECK takes `ACCESS EXCLUSIVE` + validates all rows → brief insert stall on a large `task_events`. | Use `ADD CONSTRAINT ... NOT VALID` then `VALIDATE CONSTRAINT` (weaker lock), or confirm `task_events` is small. | S9 | `0020`/`0024` pattern. Adversarial reviewer. |

> **E9** (plan-injection KV-cache) belongs to the [Planning Primitive](../../../completed/agent-modes/planning-primitive/plan.md) track and is tracked there.

**Wording/consistency minors folded in (no decision needed):** "six knobs" → **five** supervisor fields (budget is the agent-level Track-3 column, not the sub-object) in S5/S10; pin the starting `depth=1` the Supervisor `Send` passes (now in §A4.1 S6); promote `core/subagent_events.py` (+ the four `emit_*` signatures) to the shared contract so S6/S7 can import-stub before S9 lands; `coding`/`investigation` presets also seed `plan_write` (a Planning-Primitive-track tool — seeding an as-yet-unwired tool name is allowed, Track-7 precedent) in S2; off-by-one cite fixes (`createAgent :51`, `validateAgentConfig :315`).

**Re-scoping flag:** S6 and S8 are the two riskiest tasks (first use of `Send`, the first parallel super-step, custom keyed reducer, crash-resume-forward, **and** the E1/E2 cost+pause mechanism). Treat them as larger than their peers.

### Spike results — verified against `langgraph==1.0.5` (2026-06-05)

A throwaway spike reproduced E1/E2/E3 under exact prod conditions (`astream(stream_mode="updates", durability="sync")`, fan-out via `Send`, ReAct node named `"agent"`, `MemorySaver`):

- **E1 — CONFIRMED REAL.** A `Send` fan-out emits astream updates under the **fan-out node's name** (`{"subagent": ...}`), **never** `{"agent": ...}`. The cost-loop gate `if "agent" in event` matched **`False`** for every fan-out step → sub-agent token spend is silently dropped. **Fix path confirmed:** streaming the same graph with `subgraphs=True` surfaces the sub-agent's inner `"agent"` node under a namespace tuple `('subagent:<id>',)`, so S8 can either (a) stream with `subgraphs=True` and match the namespaced `"agent"` updates, or (b) have the fan-out node aggregate sub-agent `usage_metadata` and re-emit it for the parent to record. **E1 is a required S8 mechanism, not optional.**
- **E2 — ASSUMPTION HOLDS (de-risked).** After a pause, a **completed** sibling branch did **not** re-execute on resume (`pending_writes` short-circuits it) and both results were present in final state — the design's resume-forward claim is empirically correct for the LangGraph-managed (interrupt/checkpoint) pause. **Caveat preserved:** this validates pausing at the *super-step boundary* via the normal interrupt/checkpoint path. A budget-pause implemented as a bare `return` out of the astream loop *mid-super-step* is a different mechanism and is still disallowed — S6/S8 must let the fan-out super-step complete, then pause at the boundary (E2 mitigation stands, now with evidence that the boundary path is safe).
- **E3 — CONFIRMED REAL.** Two sub-agents calling `interrupt()` in one `Send` super-step produced 2 pending interrupts, and scalar `Command(resume=...)` raised exactly `RuntimeError: When there are multiple pending interrupts, you must specify the interrupt id when resuming.` **Fix confirmed:** headless sub-agents (interrupt-bearing tools filtered out, §A0 inv. 8 / S3) fan out and complete cleanly. **E3 fix ratified — implement the S3 filter.**

**Spike #2/#3 — sub-agent persistence granularity (2026-06-05).** A second pair of spikes settled how durable a fan-out sub-agent is:
- **Per-inner-turn resume — CONFIRMED.** A sub-agent built as a **checkpointed subgraph node** (sharing the parent checkpointer, reached by `Send`) persists its **inner** super-steps. Crashing a sub-agent at its 3rd inner step and resuming re-ran **only** that step (`sA=1, sB=1, sC=2`); a 3-way fan-out with one crashing branch left the other two **untouched** (`t0/t2: A=1 B=1 C=1`, `t1: A=1 B=1 C=2`). So a multi-minute sub-agent that dies mid-run resumes where it died — completed turns restored, tokens not re-spent.
- **Transcript persisted — CONFIRMED.** That same checkpointed sub-state holds the sub-agent's working messages, so the transcript survives in the namespaced sub-checkpoint → **E5 resolved, no new table.**
- **The wiring is load-bearing.** This holds **only** when the sub-agent is a subgraph *node reached by `Send`*, **not** a subgraph `ainvoke`d imperatively inside a tool/node (that runs in one parent super-step and re-runs whole on crash). → **S3 must compile the sub-agent as a checkpointed subgraph node**; the "gathered `ainvoke`" option is rejected.
- **`dispatch_subagent` also routes via `Send`.** To give Topology 1 the same per-turn durability, S4 routes the LLM's `dispatch_subagent` tool call to the shared subagent node via a **post-agent routing edge** (not the ToolNode), threading the result back as a `ToolMessage` keyed to the `tool_call_id` while keeping inner messages on a separate channel.
- *Caveat:* spikes used in-process `MemorySaver`; S3/S11 confirm once against `PostgresDurableCheckpointer` (same `pending_writes`/namespace API, expected identical).

**Spike #4 — `dispatch_subagent` via `Send` + `ToolMessage` threading, end-to-end (2026-06-05).** The S4 mechanism is validated, not just asserted. A fake-model ReAct loop emitted a `dispatch_subagent` tool call; a post-agent routing edge `Send`-ed it to the shared subagent node; all five properties held:
- **Threading:** the subagent threaded its summary back as a `ToolMessage` keyed to the original `tool_call_id`; parent sequence was clean (`Human → AI(call) → Tool(→call) → AI(final)`), loop completed.
- **Isolation (proven):** the subagent did its internal turns on a separate **`work` channel** (not in the parent schema → dropped on merge); only the summary `ToolMessage` reached the parent. The `Send` payload (`{tool_call_id, prompt}`) is the subagent's entire input, so parent history never flows in.
- **Mixed turn:** a `dispatch_subagent` call **+** a normal tool call in one turn → dispatch routed via `Send`, the normal one via the tools node, **both** `tool_call_id`s answered before the next LLM call (no provider "unanswered tool_call" error). A conditional edge returning a **mixed list of `Send(...)` + the `"tools"` node name** works.
- **Durability on the dispatch path:** a crash inside the dispatched sub-agent resumed per-inner-step (`w1` not recomputed) — same guarantee as the Supervisor path. **→ S4's contract is build-ready; the channel design (`work` internal + `messages` for the single `ToolMessage`) is the pinned recipe.**

**Spike #5 — durability against REAL Postgres, across a REAL OS-process boundary (2026-06-05; hardened after PR review).** Removes the MemorySaver caveat that hung over spikes #2–#4. *The first harness ran both "workers" in one interpreter (shared in-memory failure flag) — a reviewer correctly flagged the process boundary as simulated; the harness was rewritten and re-run.* Final form: worker A and worker B are **separate OS processes** (`subprocess`), failure injection and the execution log live **on disk**, and the only state shared between them is **Postgres itself** (`AsyncPostgresSaver`). Worker A ran a 2-way `Send` fan-out and crashed one branch at inner step `w2`; worker B (fresh process, fresh connection, fresh graph) resumed the same `thread_id`: the completed branch `t0` was untouched (`w1=w2=w3=1`), the crashed branch `t1` resumed **at `w2`** (`w1=1, w2=2, w3=1`). **→ Per-inner-turn sub-agent durability holds on the real Postgres backend across a genuine process boundary, and message state serialized/round-tripped correctly. The Postgres-confirmation caveat in S3/S11 is satisfied at the LangGraph-checkpointer level** (the repo's custom `PostgresDurableCheckpointer` still gets one integration check in S11 with the real worker).

**Spike #6 — S8 cost-attribution mechanism (2026-06-05).** Proves the E1 fix, not just the gap. Baseline (prod loop, gate on `event["agent"]`, no `subgraphs`) collected **0** sub-agent tokens — confirming E1 is real. The fix — stream with `subgraphs=True` and aggregate `usage_metadata` across all namespaces — captured **all 6 LLM calls (3 sub-agents × 2 inner turns) and the exact totals (in=600, out=300)**. **→ S8's cost mechanism is proven collectible: stream `subgraphs=True`, sum sub-agent usage, write it additively to the parent's super-step `checkpoint_id`. E1 downgraded from "blocker" to "proven mechanism, build it."**

**Net:** E1 and E3 are real and must be built as specified (S8 cost mechanism via `subgraphs=True`/aggregate-and-re-emit; S3 headless filter). E2's resume-forward is sound at the super-step boundary, and spikes #2/#3 sharpen it to **per-inner-turn** resume + free transcript persistence (E5 resolved). The wiring is pinned: **sub-agent = checkpointed subgraph node reached by `Send`, for both drivers.** None requires Pattern B. **S5–S8 are cleared to proceed on these contracts.**

**Bottom line:** the decomposition, contracts, dependency graph, and worktree-safety are execution-ready, and **all ten escalations are resolved or decided** — six spikes (durability per-turn + cross-process on real Postgres, dynamic-N fan-out, transcript persistence, `dispatch_subagent`-via-`Send` threading, and the S8 cost mechanism) plus concrete decisions (research timeout = 4 h, worker-pool ops guidance, deterministic `subtask` ids, headless sub-agents, heartbeat sink, migration `NOT VALID`). **No blockers remain.** The only things still *open* are the three build-time design items in the **Deferred Decisions Ledger** (§A12) — each ships a safe v1 now and is gated so it cannot be silently dropped. None gates kickoff. **The whole track is cleared to execute**; start anywhere the dependency graph allows (S1/S2/S3 are the natural roots).

---

## A12. Deferred Decisions Ledger (definition-of-done gate)

These items ship a **safe v1** now; the *better* version is deferred to a **data-informed decision later**. This ledger is the anti-silent-drop mechanism: **the track is NOT "complete" / archivable until every row is dispositioned** — either **Closed** ("v1 was sufficient — here's the metric") or **spun into a named follow-up task**. "Implementation done" means "v1 shipped **and** this ledger fully dispositioned"; **"v1 shipped" ≠ "fully optimized."** Triggers are observable (the v1 instrumentation), not calendar dates.

| # | Deferred decision | v1 behavior (ships now) | Observable trigger | Review gate | Disposition |
|---|---|---|---|---|---|
| D1 | **S6 — wide-fan-out checkpoint size cap/offload** | Log the serialized `subagent_results`/fan-out payload byte size past a threshold (`checkpoint.oversized` warning); **no cap**. | The warning fires in staging, or the **max checkpoint bytes** observed in the S11 load run exceeds a Postgres-write-latency budget. | **S11 acceptance** (report max bytes) → **pre-GA** of the `research` preset. | **CLOSED (v1 sufficient).** S11 measured the max serialized `subagent_results` payload at the **research-preset ceiling** (`max_fanout=5 × max_iterations=10 = 50` entries, each a *verbose-MAX* sub-agent summary ~2 KB): **116,660 bytes ≈ 0.117 MB** — well under the 1 MB threshold; the `checkpoint.oversized` warning **never fires** at the shipped caps. Evidence: `services/worker-service/tests/test_supervisor_deferred_ledger.py::test_d1_wide_fanout_checkpoint_payload_size`. Re-trigger only if the caps are raised materially or staging emits `checkpoint.oversized`. |
| D2 | **S7 — one-shot Writer reduction algorithm** | Hard cap on findings into the Writer + `log()` what's dropped (immutability invariant preserved). | **Cap-hit rate** + a **report-quality spot check** on real-ish research runs (does dropping findings degrade the report?). | **S11 acceptance** (report cap-hit rate + quality) → **pre-GA**. *Most likely to need action.* | **CLOSED (v1 sufficient).** S11 fed a corpus > `WRITER_FINDINGS_CAP=50` (73 findings) through `reduce_findings`: cap-hit fired, **23 dropped, the drop was LOGGED** (`writer.findings_reduced` — no silent truncation), and the **report-quality spot check passed** — all 50 KEPT findings remain fully citable (`resolve` → source_url, no render-error) with **byte-identical `supporting_quote`s** (immutability §A0.4 holds; select/reorder only). Degradation is "fewer findings," never "corrupted findings." Evidence: `test_supervisor_deferred_ledger.py::test_d2_writer_reduction_cap_hit_and_quality`. Map-reduce summarization remains the documented upgrade IF a real-corpus quality regression appears at pre-GA. |
| D3 | **S8 — finer in-flight cost metering** | Meter at the parent fan-out super-step boundary; per-sub-agent ceiling bounds overshoot to `max_fanout × ceiling`. | A cost-accuracy complaint, or observed per-task overshoot beyond `max_fanout × ceiling`. | **Post-GA** budget-accuracy monitoring (lowest priority). | **CLOSED (v1 sufficient).** The S8 boundary meter bills each fan-out branch's accumulated usage **exactly once** at the super-step boundary (additive, no double-count); the per-task overshoot is bounded by `max_fanout × ceiling` as designed. Real-Postgres evidence: `tests/backend-integration/test_supervisor_fanout_budget.py::test_supervisor_budget_pause_fires_at_fanout_boundary_billing_each_sibling_once` — a 5-way round bills **13,260 µ$ exactly once** (7 ledger rows, no re-bill, no `sub_agent_id` column); and the S11 **live `execute_task` resume** confirms a paused round's siblings are restored, not re-billed, on resume. Finer mid-round metering is **DEFERRED post-GA** — the bounded overshoot is the accepted v1 budget design (§A8). Re-trigger only on a post-GA cost-accuracy complaint. Evidence (analytic disposition): `test_supervisor_deferred_ledger.py::test_d3_inflight_metering_overshoot_bound`. |

**Decision points (milestone-gated, not dated):** (1) **S11 acceptance testing** is the forced checkpoint — its acceptance criteria require reporting D1/D2 metrics and dispositioning each; (2) **`research`-preset pre-GA** re-checks D1/D2 against staging metrics. D3 rides post-GA monitoring. When a row is dispositioned, update this table (Closed-with-evidence or → follow-up task id) and the §A8 "genuinely open" list.

**Disposition status (2026-06-09, S11 acceptance):** **All three rows CLOSED** (v1 sufficient — metrics above). The §A12 definition-of-done gate is satisfied. One **non-ledger observability follow-up** surfaced during S11 (does NOT gate archival): the `iteration` field on `marker.subagent.*` events is statically `0` for every round (read from the once-injected `config['configurable']['iteration']` rather than the per-round state), so S10's Console tree (`buildSubagentTree` groups on `item.event.iteration ?? 0`) collapses all rounds' sub-agents into "Round 0" while `marker.supervisor.iteration` carries the real round. The reliable round signal IS present (the `subtask` id is `"<iteration>.<index>"`). Owning tasks: S8 (advance/inject iteration) + S9 (event payload) + S10 (Console grouping). Tracked as a follow-up, not an S11 blocker.
