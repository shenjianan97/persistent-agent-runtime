<!-- AGENT_TASK_START: task-s1-api-topology-preset-config.md -->

# Task S1 — API: `topology` (immutable) + `preset` + `supervisor` Sub-Object

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"Two-layer naming and the config model"** (preset → topology → graph; topology immutable after creation; no `mode` field), **"Topology 2: Supervisor"** → **"What customers configure"** (the five supervisor knobs), and **"Presets"** (where `topology` comes from — S1 only lands the fields, S2 wires the preset→defaults mapping).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — **§A0 invariants** (esp. #2 topology immutable, #3 no `agent_config.mode` field), **§A4 / §A4.1** (the S1 output contract — names are load-bearing), **§A5**, **§A9** (orchestrator guidance, immutability discipline).
3. `docs/exec-plans/completed/phase-2/track-7/agent_tasks/task-1-agent-config-extension.md` — **copy this file's FORMAT** and treat it as the closest pattern template. S1 is structurally the same kind of change: a typed nested config sub-object on `AgentConfigRequest`, with Jackson mapping + a `ConfigValidationHelper` validator + a `canonicalizeConfig` round-trip that writes no silent defaults.
4. `services/api-service/.../model/request/AgentConfigRequest.java` — current record shape; note the existing `MemoryConfigRequest memory` and `ContextManagementConfigRequest contextManagement` nested fields with `@JsonInclude(NON_NULL)` + `@JsonProperty` snake-case keys.
5. `services/api-service/.../model/request/MemoryConfigRequest.java` and `ContextManagementConfigRequest.java` — the canonical pattern for a nested sub-object record (nullable fields, snake_case JSON keys).
6. `services/api-service/.../service/ConfigValidationHelper.java` — `validateAgentConfig` (`:314`) and its existing per-sub-object validators (`validateMemoryConfig`, `validateContextManagementConfig`, `validateModel`); follow their error-message style.
7. `services/api-service/.../service/AgentService.java` — `createAgent` (`:50`), `updateAgent` (`:130`), and `canonicalizeConfig` (`:168`). Note that `updateAgent` already fetches the current row (`agentRepository.findByIdAndTenant(...)` as `existing`) for the budget/concurrency defaults — the topology-immutability compare reuses that fetch.
8. `services/api-service/.../config/ValidationConstants.java` — where new enum sets and bounds constants belong (e.g. `MEMORY_MAX_ENTRIES_MIN/MAX`, `SANDBOX_*` follow this pattern).

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` (Java unit tests). Fix any regressions.
2. Update the status of S1 in `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` to "Done" with a one-line note.

## Context

The Agent Modes framing exposes agent *shape* via a customer-facing **preset** that sets an internal **`topology`** field (`react` | `supervisor`), plus a `supervisor` tuning sub-object for the Deep Research shape. There is **no `agent_config.mode` field** (design *Two-layer naming and the config model*; plan §A0 #3) — `topology` is the only stored shape selector, and `preset` (S2) is the API-layer selector that seeds it.

`agent_config` is JSONB (`0005_agents_table.sql:9`), so `topology`, `preset`, and the `supervisor` sub-object are **additive JSONB keys — NO DB migration**. Because Spring Boot's Jackson is configured with `FAIL_ON_UNKNOWN_PROPERTIES = true` and snake_case↔camelCase mapping, `AgentConfigRequest` must gain typed fields for these keys or requests carrying them fail schema validation before reaching the service layer, and `canonicalizeConfig` drops them on round-trip.

S1 lands **only the config surface and the immutability gate**. It does **not** seed preset defaults (that is S2), build any graph (that is the worker stream), or add Console UI (S10). This task introduces the repo's **first** config-immutability check; the existing `updateAgent` has none.

## Task-Specific Shared Contract

- `agent_config` gains three additive keys, all absent-friendly:
  - `topology: string`, optional. Enum `{react, supervisor}`. **Absent → treated as `react`** (the default; see canonicalisation note below for whether the key is written).
  - `preset: string`, nullable. The customer-facing selector. **S1 only accepts/round-trips it verbatim** — the preset→defaults mapping and the unknown-preset 400 are S2's job. (S1 may accept any string here; do not reject unknown presets in S1, or S2 has nothing to wire. If S2 ships in the same change-set, the unknown-preset check lives in S2's validator, invoked from `validateAgentConfig`.)
  - `supervisor: SupervisorConfigRequest`, nullable. The Deep Research tuning sub-object — five fields, all nullable so partial payloads are accepted:
    - `max_fanout_per_iteration: int`, bounds **[1, 20]**.
    - `max_iterations: int`, bounds **[1, 10]**.
    - `source_allowlist: list[string]`, **≤ 50 entries** (matches the existing `tool_servers` / `exclude_tools` cap).
    - `writer_style: string`, enum **`{formal_report, annotated_bullets}`**.
    - `scope_clarification_enabled: bool`.
- **Topology immutability (plan §A0 #2, §A5):** `topology` is immutable after agent creation. On `PUT /v1/agents/{id}`, compare the **canonicalised** `topology` of the incoming request against the **canonicalised** `topology` of the current persisted row; reject any **change** with a 400 carrying the message `"topology is immutable after agent creation"`. A `PUT` that omits topology (→ `react`) against a `react` agent is **not** a change and must succeed; a `PUT` that keeps topology identical must succeed. All other config stays mutable. This is the precedent — the repo currently has no immutability check anywhere in `updateAgent`.
- **Canonicalisation (same rule as `memory` / `context_management`):** `canonicalizeConfig` round-trips `topology`, `preset`, and `supervisor` **verbatim**, omitting absent keys — **no silent defaults written into the persisted row**. When `supervisor` is present, preserve all five fields verbatim (including nulls / empty `source_allowlist`). The `react` default for absent `topology` is a **read-time** convention (validator + immutability compare + worker), not a value written at write-time — absence stays absence in the stored JSON. (If, after reading `canonicalizeConfig`, you find the round-trip is cleaner by normalising an absent `topology` to the literal `"react"` *only for the immutability compare*, do that in the compare logic, not by mutating the persisted config.)
- Validation runs at both `POST /v1/agents` and `PUT /v1/agents/{id}` — reuse the same `validateAgentConfig` path for both. The immutability compare is `PUT`-only (there is no prior row at create time).
- This task does **not** change worker runtime behavior, the task-submission payload, or any read response. It only lands the API config surface plus the immutability gate.

## Affected Component

- **Service/Module:** API Service — Agents
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/SupervisorConfigRequest.java` (**new** record)
  - `services/api-service/.../model/request/AgentConfigRequest.java` (modify — add `topology`, `preset`, typed `supervisor` fields, mirroring the `memory` / `contextManagement` `@JsonInclude(NON_NULL)` + `@JsonProperty` pattern)
  - `services/api-service/.../service/ConfigValidationHelper.java` (modify — add `validateSupervisorConfig` + topology-enum validation, invoked from `validateAgentConfig` `:314`)
  - `services/api-service/.../service/AgentService.java` (modify — `canonicalizeConfig` `:168` round-trip for the three keys; `updateAgent` `:130` topology-immutability compare against the already-fetched `existing` row)
  - `services/api-service/.../config/ValidationConstants.java` (modify — add the topology enum set, `writer_style` enum set, and the four numeric/size bounds as named constants)
  - `services/api-service/src/test/java/.../` — extend the existing agent-config validation + canonicalisation test classes (the same ones Track-7 Task 1 extended), or add a new `SupervisorConfigValidationTest` / topology-immutability test alongside them.
- **Change type:** new record + modification of request model + service-layer validation + first immutability gate.

## Dependencies

- **Must complete first:** None. S1 is the root of the Supervisor Topology track and has zero file overlap with the Python worker stream (plan §A3 — "S1 (Java API) ∥ S3 (Python worker)").
- **Provides output to:** **S2** (preset defaults need these fields to exist before they can be seeded into them), **S8** (`_build_graph` branches on `agent_config.topology`), **S10** (Console mirrors `topology`/`preset`/`supervisor` fields).
- **Shared interfaces/contracts:** the JSON shape of `agent_config.{topology, preset, supervisor}` and the 400 immutability contract (plan §A4.1, S1 row — names are load-bearing).

## Implementation Specification

### New record: `SupervisorConfigRequest`

A Java `record` mirroring `MemoryConfigRequest` / `ContextManagementConfigRequest` style, with five nullable fields and snake_case JSON keys:

- `Integer maxFanoutPerIteration` — JSON `max_fanout_per_iteration`.
- `Integer maxIterations` — JSON `max_iterations`.
- `List<String> sourceAllowlist` — JSON `source_allowlist`.
- `String writerStyle` — JSON `writer_style`.
- `Boolean scopeClarificationEnabled` — JSON `scope_clarification_enabled`.

All nullable so partial payloads are accepted. Bounds/enum checks live in the validator, not as field annotations, to keep error messages consistent with the existing helper style (follow whichever convention `ContextManagementConfigRequest` + `validateContextManagementConfig` already use — prefer the helper for cross-field/enumerated messages; simple `@Min`/`@Size` annotations are acceptable for the pure numeric bounds if that matches the surrounding code).

**Unknown-property rejection** is automatic: `FAIL_ON_UNKNOWN_PROPERTIES = true` means a `supervisor` payload with an unrecognised key surfaces a 400 — no manual guard needed.

### Modify: `AgentConfigRequest`

Add three fields, each with `@JsonInclude(JsonInclude.Include.NON_NULL)` and an explicit `@JsonProperty`:

- `String topology` — JSON `topology`.
- `String preset` — JSON `preset`.
- `SupervisorConfigRequest supervisor` — JSON `supervisor`.

Match the exact annotation pattern already on the `memory` and `contextManagement` fields so absent keys are omitted on serialisation.

### Modify: `ConfigValidationHelper`

- Add topology-enum validation to `validateAgentConfig` (`:314`): when `config.topology()` is non-null, reject any value not in `{react, supervisor}` (use a new `ValidationConstants.VALID_TOPOLOGIES` set), with an error message consistent with the existing enum-style messages (e.g. the `VALID_AGENT_STATUSES` rejection). Absent topology is valid (read-time default `react`).
- Add `validateSupervisorConfig(SupervisorConfigRequest s)` invoked from `validateAgentConfig` when `s != null`:
  - `max_fanout_per_iteration` (when non-null): reject outside `[1, 20]`, message naming the bound.
  - `max_iterations` (when non-null): reject outside `[1, 10]`, message naming the bound.
  - `source_allowlist` (when non-null): reject size > 50, message naming the 50-entry cap (match the `tool_servers` / `exclude_tools` message style). Do **not** validate entry contents.
  - `writer_style` (when non-null): reject any value not in `{formal_report, annotated_bullets}` (new `ValidationConstants.VALID_WRITER_STYLES` set), enum-style message.
  - `scope_clarification_enabled`: pure boolean toggle — record typing enforces it; no further validation.
  - No cross-field validation; no coupling to `topology` (a `supervisor` sub-object on a `react` agent is accepted but inert — same posture as `pre_tier3_memory_flush` against `memory.enabled`).
- Do **not** write defaults into the canonical config — validation accepts absence or rejects an explicit out-of-range/invalid value only.

### Modify: `AgentService.canonicalizeConfig` (`:168`)

Round-trip `topology`, `preset`, and `supervisor` identically to how `memory` / `context_management` are handled (preserve verbatim when present, omit the key when absent — see the existing memory/context-management round-trip block in this method as the pattern). No defaults populated; absence stays absent. Verify by reading the persisted `agent_config` back into `AgentConfigRequest`.

### Modify: `AgentService.updateAgent` (`:130`) — topology immutability gate

`updateAgent` already fetches `existing = agentRepository.findByIdAndTenant(...)`. Add a compare:

1. Deserialise the current row's persisted `agent_config` into an `AgentConfigRequest` (or read its `topology` key) and canonicalise its topology to the read-time default (`null → "react"`).
2. Canonicalise the incoming request's topology the same way.
3. If the two differ, throw a `ValidationException` ⇒ 400 with message exactly `"topology is immutable after agent creation"`.

Place the check so it runs **before** the `update` write but consistently with the existing validation ordering in the method. Do not change the immutability semantics of any other field.

### Consumer expectations

This is a **pure config-surface + immutability** task. Do **not**: build or branch any worker graph (S8), seed preset defaults (S2), add Console UI (S10), or alter the task-submission payload / task-detail response. The only user-visible effects are: (a) `POST`/`PUT /v1/agents` now accept and persist `topology` / `preset` / `supervisor`; (b) `PUT` rejects a topology change with 400.

## Acceptance Criteria

- [ ] `POST /v1/agents` with `agent_config.topology = "supervisor"` succeeds; row reads back with `topology = "supervisor"`.
- [ ] `POST /v1/agents` with no `topology` succeeds; the persisted JSON omits the `topology` key (no default written), and the agent is treated as `react` at read time.
- [ ] `POST /v1/agents` with `agent_config.topology = "deep_research"` (or any non-`{react,supervisor}` value) fails with a 400 enum-style message. (Confirms there is **no `mode` field** and `topology` is the only shape selector — plan §A0 #3.)
- [ ] `POST /v1/agents` with a full `supervisor` sub-object (`max_fanout_per_iteration`, `max_iterations`, `source_allowlist`, `writer_style`, `scope_clarification_enabled`) round-trips **verbatim** — read-back equals submitted, fields preserved exactly.
- [ ] `POST /v1/agents` with `supervisor.max_fanout_per_iteration = 21` fails 400; `= 0` fails 400; `= 1` and `= 20` succeed.
- [ ] `POST /v1/agents` with `supervisor.max_iterations = 11` fails 400; `= 0` fails 400; `= 1` and `= 10` succeed.
- [ ] `POST /v1/agents` with `supervisor.source_allowlist` of size 51 fails 400 naming the cap; size 50 succeeds.
- [ ] `POST /v1/agents` with `supervisor.writer_style = "annotated_bullets"` and `"formal_report"` succeed; `"bullet_points"` fails 400.
- [ ] **Immutability:** `PUT /v1/agents/{id}` changing `react → supervisor` fails with 400 `"topology is immutable after agent creation"`. The mirror case `supervisor → react` also fails 400.
- [ ] `PUT /v1/agents/{id}` on a `react` agent that **omits** topology (→ `react`) succeeds (not a change); a `PUT` that keeps topology identical succeeds; a `PUT` that changes other fields (e.g. `system_prompt`) but keeps topology succeeds.
- [ ] Agents created before this task (no `topology`/`preset`/`supervisor` in their persisted JSON) remain readable and usable; a `PUT` against them with no topology succeeds — no row migration required.
- [ ] `make test` — all Java unit tests pass, including the new validation, round-trip, and immutability tests.

## Testing Requirements

- **Unit tests:** each reject case above; the supervisor round-trip preservation test; the topology-immutability `PUT` test (react→supervisor → 400) **and** its negative (identical-topology `PUT` → success), per plan §A8 ("Topology-immutability check misses a nested edit → S1 compares canonicalised `topology` value specifically; unit test PATCHes `react→supervisor` and asserts 400").
- **Regression:** existing agent-creation/update and canonicalisation tests pass unchanged; no existing `validateAgentConfig` call site breaks; existing agents with no topology still round-trip.
- **No DB tests needed** — the persisted JSON is bytes in `agent_config`; no schema change.

## Constraints and Guardrails

- **NO DB migration** — `agent_config` is JSONB (`0005_agents_table.sql:9`); the three keys are additive (plan §A4).
- **NO `agent_config.mode` field** — `topology` is the only stored shape selector; `preset` is the selector that seeds it (S2). Do not introduce a `mode` key under any name (plan §A0 #3, design *Two-layer naming and the config model*).
- **Do not write defaults into the persisted config** — absence stays absent; the `react` default is read-time only (same rule as `memory` / `context_management`).
- **Topology is immutable** — the compare is on the **canonicalised** value; do not allow any path (nested edit, status-only PUT, field reorder) to mutate it.
- Do **not** seed preset→defaults here (S2), build/branch any graph (S8), or add Console UI (S10).
- Do not introduce new global Jackson config (no `IGNORE_UNKNOWN_PROPERTIES`). Typed fields are the correct fix.
- Error messages must match the existing helper style; do not invent new error codes.
- Do not validate `source_allowlist` entry contents (customers may name tools/stores not yet wired) — only the 50-entry cap.

## Assumptions

- Jackson is configured for snake_case JSON ↔ camelCase records (already in place — `memory` / `context_management` rely on it).
- `updateAgent` already loads the current row (`existing`) for budget/concurrency defaults; the immutability compare reuses that fetch — no extra query needed.
- `preset` is accepted as a free string in S1; the unknown-preset 400 and preset→defaults seeding are S2's responsibility (a `supervisor` agent can be created directly via `topology` without a `preset`, which S2 must not break).
- No feature flag required — the fields are opt-in-by-absence (missing keys = `react` agent with no supervisor tuning).

<!-- AGENT_TASK_END: task-s1-api-topology-preset-config.md -->
