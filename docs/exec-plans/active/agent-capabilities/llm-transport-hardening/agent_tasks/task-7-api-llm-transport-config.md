<!-- AGENT_TASK_START: task-7-api-llm-transport-config.md -->

# Task 7 — API: `agent_config.llm_transport` Sub-Object

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — what knobs operators need.
3. `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` — current shape, including the existing `MemoryConfigRequest memory` and Track 7's `ContextManagementConfigRequest contextManagement` fields.
4. `services/api-service/src/main/java/com/persistentagent/api/model/request/MemoryConfigRequest.java` — canonical pattern for a nested optional sub-object with snake-case JSON keys.
5. `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` — existing `validateAgentConfig`, `validateMemoryConfig`, `validateContextManagementConfig`.
6. `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` — `canonicalizeConfig` round-trip semantics (no silent defaults written on absent fields).
7. Track 7's `task-1-agent-config-extension.md` (in `docs/exec-plans/completed/phase-2/track-7/agent_tasks/`) — the closest precedent for the exact change shape this task makes.
8. `services/worker-service/executor/transport.py` (Task 1's output) — defines the fields and bounds this task must mirror exactly.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make api-test` (or the project's narrowest equivalent — locate via `make help`).
2. Update `progress.md` row 7 to "Done".

## Context

Task 1's resolver consumes `agent_config.llm_transport` if present. This task is the API-side surface that lets operators set those overrides via `POST /v1/agents` and `PUT /v1/agents/{agent_id}`. It is exactly the same shape change Track 7 made for `context_management` — read that task spec first.

## Task-Specific Shared Contract

- `agent_config.llm_transport` has three optional override fields. **There is no `enabled` field** — the sub-object is purely tuning; absent fields fall back to platform defaults documented in Task 1.
  - `connect_timeout_s: number`, optional, range `[1, 60]`.
  - `read_timeout_s: number`, optional, range `[10, 900]`.
  - `max_output_tokens: integer`, optional, range `[256, 200_000]`.
- **Bounds match Task 1 exactly.** If they drift, the worker resolver will clamp + warn — but the API should be the first line of defense.
- **Canonicalisation:** `AgentService.canonicalizeConfig` round-trips the sub-object as-is. Absent sub-object stays absent in the persisted JSON; present sub-object preserves field-level absence (an absent `read_timeout_s` is not silently filled with the default).
- **Unknown-property rejection.** Spring Boot's Jackson is configured with `FAIL_ON_UNKNOWN_PROPERTIES = true`. Any client sending `enabled` or any other field gets a 400 with `"Unrecognized field 'enabled'"` automatically. No manual guard needed.
- This task does not change any worker behavior. Worker wiring lands when Tasks 1–4 merge.

## Affected Component

- **Service/Module:** API Service — Agents
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/LlmTransportConfigRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` (modify — add typed `llmTransport` field)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` (modify — add `validateLlmTransportConfig` invoked from `validateAgentConfig`)
  - `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` (modify — `canonicalizeConfig` round-trip)
  - `services/api-service/src/test/java/.../AgentConfigValidationTest.java` (extend)
  - `services/api-service/src/test/java/.../AgentServiceCanonicalizeTest.java` (extend)
- **Change type:** new record + modification of request model + service-layer validation + test extensions

## Dependencies

- **Must complete first:** None. Task 1 (worker resolver) does not depend on this task; it consumes whatever's in the loaded dict, with platform defaults when absent.
- **Provides output to:** Task 4 (worker reads the persisted sub-object via the existing agent_repository → resolver chain), future Console task (if a UI for these overrides is added — currently out of scope).
- **Shared interfaces/contracts:** the JSON shape of `agent_config.llm_transport`.

## Implementation Specification

### New record: `LlmTransportConfigRequest`

Mirror the `MemoryConfigRequest` and `ContextManagementConfigRequest` style:

- `Double connectTimeoutS` — nullable. Snake-case JSON key `connect_timeout_s`.
- `Double readTimeoutS` — nullable. Snake-case JSON key `read_timeout_s`.
- `Integer maxOutputTokens` — nullable. Snake-case JSON key `max_output_tokens`.

All fields nullable so partial payloads are accepted.

### Modify: `AgentConfigRequest`

Add `LlmTransportConfigRequest llmTransport` — nullable. Snake-case JSON key `llm_transport`. `@JsonInclude(JsonInclude.Include.NON_NULL)`.

### Modify: `ConfigValidationHelper.validateAgentConfig`

Add `validateLlmTransportConfig(LlmTransportConfigRequest t)` invoked when `t != null`:

- `connectTimeoutS` (when non-null): must be in `[1, 60]`. On out-of-range, throw the same `BadRequestException` (or whatever the existing helpers throw) with message `"connect_timeout_s must be between 1 and 60"`.
- `readTimeoutS` (when non-null): must be in `[10, 900]`. Message `"read_timeout_s must be between 10 and 900"`.
- `maxOutputTokens` (when non-null): must be in `[256, 200_000]`. Message `"max_output_tokens must be between 256 and 200000"`.

### Modify: `AgentService.canonicalizeConfig`

When the request carries `llmTransport`, preserve it in the persisted JSON exactly as received (no field-level default fills). When absent, omit the key.

### Tests

Extend `AgentConfigValidationTest`:

- Each field at min, max, just-below-min, just-above-max.
- Absent sub-object accepted.
- Empty sub-object accepted (all fields null).
- Unknown field inside `llm_transport` (e.g., `"foo": 1`) → 400.

Extend `AgentServiceCanonicalizeTest`:

- Round-trip a full sub-object.
- Round-trip a partial sub-object (only `read_timeout_s`).
- Round-trip an absent sub-object → persisted JSON has no `llm_transport` key.

## Acceptance Criteria

- [ ] `POST /v1/agents` and `PUT /v1/agents/{agent_id}` accept `agent_config.llm_transport` with the three documented fields.
- [ ] Out-of-range values are rejected with a clear 400 message.
- [ ] Canonicalisation preserves field-level absence.
- [ ] All extended tests pass.
- [ ] Bounds in this task exactly match the constants exported by Task 1's `executor/transport.py`. Document the cross-reference inline as a code comment ("must match Worker's transport.py bounds; see plan §A2").

## Out of Scope

- Worker-side consumption (Task 1 + 4).
- Console form for editing these fields (deferred to a follow-up after operator demand).
- Per-model defaults (e.g., bumping the default for known-slow models) — out of scope for this hardening cycle.

<!-- AGENT_TASK_END -->
