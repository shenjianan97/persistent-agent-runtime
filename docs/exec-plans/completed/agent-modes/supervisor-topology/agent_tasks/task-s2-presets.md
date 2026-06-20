<!-- AGENT_TASK_START: task-s2-presets.md -->

# Task S2 — `PresetDefaults`: Named Default Bundles Applied at Agent Creation

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"Presets"** (the preset table — `chat` / `coding` / `investigation` / `research` / `workflow_runner`, their topologies and notable defaults; "Presets are *starting points*; customers override individual fields"), **"Two-layer naming and the config model"** (preset → topology → graph; **no `mode` field** — the preset *sets* topology), and **"Topology 2: Supervisor" → "What customers configure"** (the supervisor sub-object the `research` preset seeds).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — **§A0 invariants** (#3 no `mode` field; preset→topology), **§A1 #2** ("Presets (API)"), **§A4.1 S2 row** (the canonical S2 output contract — names load-bearing: `research` seeds `max_concurrent_tasks=2`, `topology=supervisor`, web tool allowlist, fan-out width 5; unknown preset → 400), **§A5**, **§A9**.
3. `docs/exec-plans/active/agent-modes/supervisor-topology/agent_tasks/task-s1-api-topology-preset-config.md` — **S2 depends on S1.** S1 adds the `topology` / `preset` / `supervisor` fields and the topology-immutability gate; S2 seeds defaults *into* those fields. Read S1's contract so you seed the same shapes S1 validates.
4. `docs/exec-plans/completed/phase-2/track-7/agent_tasks/task-1-agent-config-extension.md` — **FORMAT template** and the closest pattern for a config-layer change (validator + `canonicalizeConfig` round-trip discipline; no silent defaults written *except* — and this is S2's one deliberate divergence — the explicit preset-seeded defaults).
5. `services/api-service/.../service/AgentService.java` — `createAgent` (`:50`) and `canonicalizeConfig` (`:168`). **Critical:** note that `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour` are **NOT in `agent_config`** — they are columns on `agents`, set in `createAgent` from `AgentCreateRequest.{maxConcurrentTasks,budgetMaxPerTask,budgetMaxPerHour}` (`:60`-ish) with the `DEFAULT_*` fallbacks. So a preset that seeds concurrency/budget must influence **those request-level values in `createAgent`**, not the JSONB config — see Implementation Specification.
6. `services/api-service/.../model/request/AgentCreateRequest.java` — the `max_concurrent_tasks` / `budget_max_per_task` / `budget_max_per_hour` fields and their `@Min` annotations.
7. `services/api-service/.../service/ConfigValidationHelper.java` — `validateAgentConfig` (`:314`); add the unknown-preset 400 here (or in a thin `validatePreset` helper invoked from it).
8. `services/api-service/.../config/ValidationConstants.java` — `DEFAULT_MAX_CONCURRENT_TASKS` (= 5), `DEFAULT_BUDGET_MAX_PER_TASK`, `DEFAULT_BUDGET_MAX_PER_HOUR`, `BASE_PLATFORM_TOOLS`, `SANDBOX_TOOLS` — the existing default/constant home; preset constants belong nearby or in `PresetDefaults` itself.
9. `infrastructure/database/migrations/0007_scheduler_and_budgets.sql` — confirms `max_concurrent_tasks` DEFAULT 5, `budget_max_per_task` DEFAULT 500000, `budget_max_per_hour` DEFAULT 5000000 (the column-level defaults a preset overrides at the request layer).

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` (Java unit tests). Fix any regressions.
2. Update the status of S2 in `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` to "Done" with a one-line note.

## Context

Presets are **platform-owned named bundles of defaults** applied **at agent creation** (design *Presets*). A preset is the customer-facing selector that **sets `topology`** and seeds a starting configuration — tool allowlist, concurrency, budgets, and sub-object defaults — so the customer isn't configuring from a blank page. There is **no `mode` field**: the preset is the only customer-facing shape selector, and it works by seeding the internal `topology` field S1 introduced (plan §A0 #3, design *Two-layer naming and the config model*).

The load-bearing rule (design *Presets*: "Presets are *starting points*; customers override individual fields"): **explicit request fields always override preset defaults.** A preset only fills a field the request left absent. This is S2's one deliberate divergence from the "no silent defaults" rule the rest of the config layer follows — and it is bounded: preset seeding happens **once, at `createAgent`**, never on `PUT` (topology is immutable per S1, and re-seeding on update would silently re-introduce defaults a customer had cleared).

S2 depends on S1: the `topology` / `preset` / `supervisor` fields and their validation must exist before S2 can seed them.

## Task-Specific Shared Contract

- A new `PresetDefaults` mapping (`service/PresetDefaults.java` **or** `config/PresetDefaults.java`) maps a **preset name → a seeded-defaults bundle**. The bundle can seed: `topology`, the tool allowlist (`agent_config.allowed_tools`), the agent-level `max_concurrent_tasks`, budgets (`budget_max_per_task` / `budget_max_per_hour`), the per-task-default **`task_timeout_seconds`** (research raises it — E7), and `agent_config` sub-object defaults (notably the `supervisor` sub-object for `research`). Seed each through the same request-value → preset-value → `DEFAULT_*` fallback chain `max_concurrent_tasks` uses; `task_timeout_seconds` is a per-task / agent-default field, **not** a `supervisor` sub-field.
- **Apply point:** at **agent creation only** — in `createAgent` / `canonicalizeConfig`, after S1's validation, before the row is persisted. Never on `PUT`/`updateAgent` (topology is immutable; no re-seeding).
- **Override rule (decided, load-bearing):** for every field a preset can seed, an **explicit value on the request wins**. The preset fills only fields the request left absent (`null`/unset). A request that sets `topology` explicitly *and* names a preset whose topology differs is a contradiction — reject it 400 (you cannot both pick `research`→supervisor and set `topology=react`), OR let the explicit `topology` win per the override rule; **choose the 400** (clearer, and consistent with topology being a deliberate shape choice — document whichever you pick in the code + a test).
- **Presets (design *Presets* table):**
  - `chat` — `topology = react`, planning **off**, light/customer-defined tools, small per-turn budget. Lightest bundle.
  - `coding` — `topology = react`, `dispatch_subagent` **and `plan_write`** enabled in the tool allowlist (design's *Presets* table marks coding "Planning on"), coding/sandbox tools (Track 8 surface), aggressive compaction, **larger** per-task budget.
  - `investigation` — `topology = react`, broad tool allowlist (search/sandbox/BYOT), `dispatch_subagent` **and `plan_write`** enabled (design's *Presets* table marks investigation "Planning on").
  - **NOTE (E6 — `max_concurrent_tasks=2` is per-AGENT, not a starvation guard):** the preset's `max_concurrent_tasks=2` sets the per-agent DB column `agents.max_concurrent_tasks` (admission for *this* agent). It does **NOT** bound cross-tenant worker-slot starvation: that limit is the per-**worker-process** `config.max_concurrent_tasks` `asyncio.Semaphore` (`core/poller.py:129`), which a handful of multi-minute fan-outs from *different* agents can still saturate. The real mitigation is **worker-pool isolation / sizing** (ops/rollout — plan §A11-E6 / §A6.4), not this preset value. Keep the preset at `2` (correct for per-agent admission), but do not represent it as the cross-tenant guard.
  - **`plan_write` seeding NOTE:** `plan_write` is delivered by the Planning Primitive track task **P1**. Seeding a not-yet-wired tool name in the allowlist is **acceptable** (Track-7 precedent: allowlists may reference tool names ahead of the runtime, same as `dispatch_subagent` is seeded ahead of S4).
  - `research` — `topology = supervisor`, tool allowlist seeded with the **web** tools (`web_search` + `read_url` — use the repo's actual built-in names; verify against `ValidationConstants` / the worker tool registry before hard-coding), `supervisor.max_fanout_per_iteration = 5` (fan-out width 5), `supervisor.writer_style = formal_report`, **`max_concurrent_tasks = 2`** (per-agent admission; see the E6 NOTE below — deliberate, not a typo, but not the cross-tenant guard it might look like), and **`task_timeout_seconds = 14400`** (4 h — the decided default per plan §A11-E7 / §A6.5; tunable; vs. the 3600s platform default).
    - **E7 — `task_timeout_seconds`:** a whole Deep Research run is **one task**. The reaper dead-letters any task where `timeout_reference_at + task_timeout_seconds < NOW()` (`core/reaper.py:98`), and `timeout_reference_at` is set once at creation — this clock runs **independent of the (healthy) lease heartbeat**, so a wide multi-iteration fan-out that exceeds 3600s is **dead-lettered mid-run**, stranding all accumulated checkpoint state (`subagent_results` / findings) as a non-resumable failure. The `research` preset therefore seeds a much larger `task_timeout_seconds`. **`task_timeout_seconds` is a per-task submission field / agent default — it is NOT inside the `supervisor` sub-object.**
      - **Plumbing (verify before implementing):** today `task_timeout_seconds` is *only* a per-task field on `TaskSubmissionRequest`, defaulted at submission time from `ValidationConstants.DEFAULT_TASK_TIMEOUT_SECONDS = 3600` (`TaskService.java:135`); there is **no** agent-level column (it lives only on `tasks` — migration `0001`). S2 is **creation-time and NO-migration** (see Constraints), so seed the preset's value as an **agent-level default in `agent_config` JSONB** (e.g. `agent_config.task_timeout_seconds`) and have `TaskService` fall back to it when the submission omits the field (request value → agent-config default → `DEFAULT_TASK_TIMEOUT_SECONDS`). If wiring that `TaskService` fallback is out of S2's scope, **flag it explicitly** in the task note and the PR as the dependent piece, and keep S2's contribution to seeding the JSONB default + its test — do not silently rely on a column that doesn't exist.
      - Size it from the per-sub-agent ceiling × `max_iterations` × `max_fanout_per_iteration` wall-clock (the worst-case serial-ish duration of a full fan-out), and document the arithmetic in a comment. (S8 may *also* reset `timeout_reference_at` per super-step; S2 owns the preset-default side of the §A6.5 fix — either-or-both is acceptable, but the preset seed is the S2 deliverable here.)
  - `workflow_runner` — **deferred target.** Workflow is Phase 3 / out of scope for this plan (plan §A9 "Do NOT build"; design *Implementation tracks* — Workflow is a Phase-3 candidate). The preset **may be declared** in `PresetDefaults` (so the name is reserved and recognised), but its target (`execute_workflow` / direct `workflow_id` submission) does not exist yet. **Document this explicitly** in the `PresetDefaults` entry and the task note: either (a) declare it as a recognised name that seeds nothing actionable yet (a `react` placeholder), or (b) omit it and have the validator return a clear "preset not yet available" 400. Pick one, document the choice, and do **not** wire any Workflow machinery.
- **Unknown preset → 400.** A `preset` value not in the known set (the declared presets above) is rejected at validation with a 400 naming the valid presets — consistent with the existing enum-rejection message style. (S1 accepts `preset` as a free string; S2 is where the known-set check lands. Ensure the two compose: S1's `validateAgentConfig` and S2's preset check run on the same path.)
- The seeded `topology` / `supervisor` values must satisfy **S1's** validator (bounds/enums) — i.e. the `research` preset's `max_fanout_per_iteration = 5` is within `[1,20]`, `writer_style = formal_report` is in the enum, etc. Preset defaults are not exempt from S1 validation.

## Affected Component

- **Service/Module:** API Service — Agents (Presets)
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/service/PresetDefaults.java` (**new**) — or `config/PresetDefaults.java`. The preset→bundle mapping + the apply/merge logic (or the merge logic lives in `AgentService` and `PresetDefaults` is the data table; either is fine — keep the override rule in one place and unit-test it).
  - `services/api-service/.../service/AgentService.java` (modify — `createAgent` `:50` / `canonicalizeConfig` `:168`: apply preset defaults at creation; seed `max_concurrent_tasks` / budgets at the request layer where `DEFAULT_*` fallbacks currently apply).
  - `services/api-service/.../service/ConfigValidationHelper.java` (modify — unknown-preset 400, in `validateAgentConfig` or a `validatePreset` helper).
  - `services/api-service/.../config/ValidationConstants.java` (modify — the known-preset set + any preset constant values, unless you keep them inside `PresetDefaults`).
  - `services/api-service/src/test/java/.../` — new `PresetDefaultsTest` + extensions to the agent-creation tests asserting seed + override behavior.
- **Change type:** new mapping/service + creation-time apply + validation (unknown-preset 400).

## Dependencies

- **Must complete first:** **S1** (the `topology` / `preset` / `supervisor` fields + validation must exist before S2 can seed them). Plan §A3: `S1 ─► S2`.
- **Provides output to:** S10 (Console preset selector reads the known-preset set + shows seeded defaults), S8 (the `research` preset is the only path that produces `topology = supervisor` in practice, so its defaults shape what `_build_graph` sees), ops/rollout (plan §A6 #3 — supervisor ships dark until the `research` preset is chosen).
- **Shared interfaces/contracts:** plan §A4.1 S2 row (load-bearing): `PresetDefaults` maps name → defaults; explicit fields override; `research` seeds `max_concurrent_tasks=2`, `topology=supervisor`, web allowlist, fan-out width 5; unknown preset → 400.

## Implementation Specification

### New: `PresetDefaults`

A platform-owned table mapping each known preset name to its seeded-defaults bundle. A bundle expresses, per preset: the `topology`, an `allowed_tools` set to seed, `max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour`, and any `agent_config` sub-object defaults (the `supervisor` sub-object for `research`). Use the design *Presets* table as the source of truth for which fields each preset sets; leave a field **unseeded** when the design says "customer-defined" (e.g. `chat`'s tools).

For values not pinned by the design (exact `coding`/`research` budget numbers, exact `dispatch_subagent`/coding tool names), pick sensible v1 defaults grounded in the existing `DEFAULT_*` constants (`config/ValidationConstants.java`) and the actual built-in tool names (verify against `ValidationConstants.BASE_PLATFORM_TOOLS` / `SANDBOX_TOOLS` and the worker tool registry — do **not** invent tool names). Document each chosen number in a comment. Keep the design-pinned values exact: `research` → `topology=supervisor`, fan-out width 5, `formal_report` writer, `max_concurrent_tasks=2`.

### Apply at creation (override rule)

In `createAgent` (after S1 validation, before persist), merge preset defaults under the **explicit-wins** rule:

- For each `agent_config` field a preset seeds (`topology`, `allowed_tools`, `supervisor` sub-fields): if the request left it absent, fill from the preset; if the request set it, keep the request's value.
- For the **request-level** columns (`max_concurrent_tasks`, `budget_max_per_task`, `budget_max_per_hour`): these currently fall back to `DEFAULT_*` when the request omits them (`createAgent` `:60`-ish). Insert the preset's value as the fallback *between* "request explicit" and "`DEFAULT_*`": request value → else preset value → else `DEFAULT_*`. So `research` with no explicit `max_concurrent_tasks` lands `2`; with no preset, behavior is unchanged (`DEFAULT_MAX_CONCURRENT_TASKS = 5`).
- Run the merge **before** `canonicalizeConfig` serialises, so seeded `agent_config` keys round-trip through S1's verbatim canonicalisation. (Seeded values are real, persisted values — this is the deliberate exception to "no silent defaults": a preset's whole purpose is to write a starting config.)
- Topology vs. explicit-topology contradiction: if the request sets `topology` explicitly to a value that conflicts with the preset's topology, reject 400 (see Shared Contract). A request that names a preset and leaves `topology` absent gets the preset's topology.

### Validation (unknown preset → 400)

In `validateAgentConfig` (or a `validatePreset` helper it calls): when `preset` is non-null and not in the known-preset set, throw `ValidationException` ⇒ 400 naming the valid presets (enum-style message). A `null`/absent preset is valid (direct `topology` config without a preset is supported — S1 guarantees this).

### `workflow_runner` handling

Declare-or-defer per the Shared Contract; **document the choice in code**. Wire **no** Workflow machinery, `execute_workflow` tool, or `workflow_id` plumbing — all Phase 3 (plan §A9 "Do NOT build").

### Consumer expectations

S2 is a **creation-time config-seeding + validation** task. Do **not**: build/branch any worker graph (S8), add the supervisor runtime (the Supervisor Topology track worker tasks), add Console UI (S10), re-seed on `PUT`, or touch topology immutability (S1 owns it). The user-visible effect: `POST /v1/agents` with `preset = "research"` (and nothing else) yields a supervisor agent with `max_concurrent_tasks=2`, web tools, fan-out 5, formal-report writer — all overridable per field.

## Acceptance Criteria

- [ ] `POST /v1/agents` with `agent_config.preset = "research"` and no other shape fields → persisted agent has `topology = supervisor`, `supervisor.max_fanout_per_iteration = 5`, `supervisor.writer_style = formal_report`, the web tool allowlist, and **`max_concurrent_tasks = 2`** (verify on the `agents` row, not just `agent_config`).
- [ ] `POST /v1/agents` with `preset = "research"` and no explicit timeout → the persisted agent carries a seeded **`task_timeout_seconds = 14400`** (4 h, tunable; in `agent_config` JSONB) — **not** inside the `supervisor` sub-object. An explicit value still wins (explicit-wins). If the `TaskService` submission-time fallback to this agent default is wired in S2, also assert a task submitted against a research agent (omitting `task_timeout_seconds`) lands the seeded value, not 3600; if that fallback is deferred, the task note records it as the dependent follow-up.
- [ ] `POST /v1/agents` with `preset = "research"` AND an explicit `supervisor.max_fanout_per_iteration = 8` → persisted value is **8** (explicit overrides preset).
- [ ] `POST /v1/agents` with `preset = "research"` AND explicit `max_concurrent_tasks = 4` → persisted `max_concurrent_tasks = 4` (explicit overrides preset's 2).
- [ ] `POST /v1/agents` with `preset = "chat"` → `topology = react`, light bundle, no `supervisor` sub-object seeded.
- [ ] `POST /v1/agents` with `preset = "coding"` → `topology = react`, `dispatch_subagent` **and `plan_write`** present in the seeded allowlist, larger budget than `chat`.
- [ ] `POST /v1/agents` with `preset = "investigation"` → `topology = react`, broad allowlist incl. `dispatch_subagent` **and `plan_write`**.
- [ ] `POST /v1/agents` with `preset = "bogus_preset"` → 400 naming the valid presets.
- [ ] `POST /v1/agents` with **no** `preset` → behavior unchanged from pre-S2 (no seeding; `max_concurrent_tasks` falls back to `DEFAULT_MAX_CONCURRENT_TASKS = 5`); a direct `topology = supervisor` with no preset still works (S1 path).
- [ ] `POST /v1/agents` with `preset = "research"` AND explicit `topology = "react"` (contradiction) → 400 (per the documented choice).
- [ ] A preset-seeded `supervisor` sub-object passes **S1's** bounds/enum validation (e.g. seeded `max_fanout_per_iteration = 5` ∈ [1,20]) — preset defaults are not exempt.
- [ ] `workflow_runner`: per the documented choice — either recognised (seeds nothing actionable) or returns a clear "not yet available" 400; **no** Workflow machinery is wired.
- [ ] Preset seeding does **not** run on `PUT /v1/agents/{id}` (no re-seeding; S1's topology-immutability still holds).
- [ ] `make test` — all Java unit tests pass, including `PresetDefaultsTest` (seed + override + unknown-preset).

## Testing Requirements

- **Unit tests:** `PresetDefaultsTest` asserting each preset's seeded bundle; the override rule (explicit request field wins, per `supervisor` sub-field and per request-level column); unknown-preset 400; the `research`→`max_concurrent_tasks=2` seed on the `agents` row; the explicit-topology-vs-preset contradiction 400; the no-preset no-op (unchanged `DEFAULT_*` behavior).
- **Regression:** existing agent-creation tests (no preset) pass unchanged; existing `validateAgentConfig` call sites unaffected; S1's tests still pass.
- **No DB tests / migration** — seeded values are persisted JSONB + existing `agents` columns; no schema change.

## Constraints and Guardrails

- **NO DB migration** — presets write into existing `agent_config` JSONB and existing `agents` columns (plan §A4).
- **NO `agent_config.mode` field** — the preset *sets* `topology`; it is not stored as a separate mode (plan §A0 #3, design *Two-layer naming and the config model*).
- **Explicit request fields ALWAYS override preset defaults** — never write a preset default that a request value cannot override. The preset fills only absent fields.
- **Seed at creation only** — never re-seed on `PUT`/`updateAgent` (topology is immutable; re-seeding re-introduces cleared defaults).
- **Topology immutability is S1's gate** — S2 does not change it; S2 only chooses the *initial* topology via the preset.
- **Do NOT build Workflow** — `workflow_runner` declares a name at most; no `execute_workflow`, no `workflow_id` submission, no step-list machinery (Phase 3 — plan §A9).
- Verify all seeded tool names against the actual repo tool registry / `ValidationConstants`; do not invent tool names.
- Error messages match the existing enum-rejection style; no new error codes.

## Assumptions

- S1 has landed: `topology` / `preset` / `supervisor` fields exist on `AgentConfigRequest`, are validated, and round-trip verbatim through `canonicalizeConfig`; `PUT` rejects topology changes.
- `max_concurrent_tasks` / budgets are `agents` columns set in `createAgent` from `AgentCreateRequest` with `DEFAULT_*` fallbacks — preset seeding hooks that fallback chain, not the JSONB.
- The built-in tool names the presets reference (`dispatch_subagent`, `web_search`/`read_url`, coding/sandbox tools) exist or are reserved in the repo's tool registry — verify before hard-coding; `dispatch_subagent` itself is delivered by S4 (the preset may seed the name ahead of the runtime, as Track-7 allowed unwired tool names in allowlists).
- No feature flag — presets are opt-in by the customer naming one; absence preserves pre-S2 behavior.

<!-- AGENT_TASK_END: task-s2-presets.md -->
