<!-- AGENT_TASK_START: task-2-agent-config-extension.md -->

# Task 2 — Agent Config Extension: `memory` Sub-Object

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Agent table extension" and "Validation and Consistency Rules".
2. `services/api-service/.../model/request/AgentConfigRequest.java` — current shape, to see how existing nested objects (e.g., `SandboxConfigRequest`) are wired.
3. `services/api-service/.../model/request/SandboxConfigRequest.java` — canonical pattern for a nested config sub-object with Jackson mapping and validation.
4. `services/api-service/.../service/ConfigValidationHelper.java` — existing `validateAgentConfig` and its style. Track 5 adds a `validateMemoryConfig` helper alongside.
5. `services/api-service/.../service/AgentService.java` — `canonicalizeConfig` (the method referenced by the design doc). The round-trip must preserve the new `memory` sub-object.
6. Track 4 task `task-3-agent-config-extension.md` in `docs/exec-plans/completed/phase-2/track-4/agent_tasks/` — same shape of change for the `tool_servers` field; use as a pattern template.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` (Java + worker unit tests). Fix any regressions.
2. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Memory is opt-in per agent via `agent_config.memory.enabled` (default `false`). Customers also choose an optional summarizer model and an optional per-agent `max_entries` soft cap. These three fields live inside a nested `memory` object on the existing `agents.agent_config` JSONB column — **no new DB columns**.

Because Spring Boot's default Jackson is configured with `FAIL_ON_UNKNOWN_PROPERTIES = true`, the `AgentConfigRequest` record MUST be extended with a typed `MemoryConfigRequest memory` field. Without this extension, requests carrying `memory` fail schema validation before reaching the service layer, and `AgentService.canonicalizeConfig` drops the field on round-trip.

## Task-Specific Shared Contract

- `agent_config.memory` has three optional fields — all three must be absent-friendly:
  - `enabled: bool`, default `false`. Absent = `false`.
  - `summarizer_model: string`, optional. When present, must reference an active row in `models` for the configured provider. When absent, the worker falls back to a platform-default summarizer.
  - `max_entries: int`, optional. When present, must be an integer in `[100, 100_000]`. When absent, the worker applies the platform default of `10_000`.
- The platform default for `max_entries` is `10_000`. Lower bound `100` exists so the Console UX does not starve. Upper bound `100_000` exists so HNSW rebuild stays within operational tolerances (see design doc "Scale and Operational Plan").
- Canonicalisation: `AgentService.canonicalizeConfig` must round-trip the `memory` sub-object exactly as stored (no silent defaults written to the row — defaults apply at read time in the worker and the validator, not at write time).
- Memory-disabled agents (`enabled=false` or `memory` absent) must behave identically to pre-Track-5 behaviour. This task does not change worker runtime behaviour; it only lands the config surface. Subsequent tasks (5–8) enforce the gating.
- Validation runs at both `POST /v1/agents` and `PUT /v1/agents/{agent_id}` — reuse the helper for both paths.

## Affected Component

- **Service/Module:** API Service — Agents
- **File paths:**
  - `services/api-service/.../model/request/MemoryConfigRequest.java` (new)
  - `services/api-service/.../model/request/AgentConfigRequest.java` (modify — add typed `memory` field)
  - `services/api-service/.../service/ConfigValidationHelper.java` (modify — add `validateMemoryConfig` invoked from `validateAgentConfig`)
  - `services/api-service/.../service/AgentService.java` (modify — `canonicalizeConfig` round-trip)
  - `services/api-service/src/test/java/.../AgentConfigValidationTest.java` (new or extend existing)
  - `services/api-service/src/test/java/.../AgentServiceCanonicalizeTest.java` (extend if present, else add)
- **Change type:** new record + modification of request model + service-layer validation

## Dependencies

- **Must complete first:** Task 1 (Infra + Migration) — not because this task touches schema, but because the plan requires Task 1 to land first as the sequencing anchor.
- **Provides output to:** Tasks 6 (worker write path — gates on `memory.enabled` and reads `summarizer_model` / `max_entries`), 7 (worker tools — gates registration), 9 (Console — surfaces the toggle).
- **Shared interfaces/contracts:** The JSON shape of `agent_config.memory`.

## Implementation Specification

### New record: `MemoryConfigRequest`

Create a Java `record` (or equivalent class — match `SandboxConfigRequest` style) with:

- `Boolean enabled` — nullable, treated as `false` when absent.
- `String summarizerModel` — nullable. Snake-case JSON key `summarizer_model` (match Jackson config).
- `Integer maxEntries` — nullable. Snake-case JSON key `max_entries`.

All fields nullable so partial payloads are accepted. The validator enforces bounds; absence is always valid.

### Modify: `AgentConfigRequest`

Add a field:

- `MemoryConfigRequest memory` — nullable.

Jackson configuration must accept `memory` as a recognised key (no `@JsonIgnoreProperties(ignoreUnknown)` additions — the record field itself recognises it). Match the Jackson pattern `SandboxConfigRequest sandbox` uses on the same record.

### Modify: `ConfigValidationHelper.validateAgentConfig`

Add a new helper `validateMemoryConfig(MemoryConfigRequest memory, String provider)` invoked from `validateAgentConfig` when `memory != null`:

1. When `memory.summarizerModel` is non-null and non-empty:
   - Resolve the model via the existing `models`-table lookup the helper already uses for chat-model validation (match whatever `validateModel` does for the main `model` field).
   - Reject when the row is not `active`, when the provider does not match the agent's provider, or when provider credentials are not resolvable.
   - Error message should be consistent with the existing "unknown model" / "disabled model" messages.
2. When `memory.maxEntries` is non-null:
   - Reject values `< 100` or `> 100_000` with a message naming both bounds.
3. When `memory.enabled` is non-null and the agent's overall config otherwise lacks an `agentId` / `provider` — no change needed; leave the existing top-level validation path to catch it.

Do NOT write defaults into the canonical config (see next section). Validation either accepts absence or rejects an explicit out-of-range / unresolvable value.

### Modify: `AgentService.canonicalizeConfig`

Whatever shape the method uses today to copy `AgentConfigRequest` into the persisted JSON (field-by-field mapping, builder, or a spread), add the `memory` sub-object to the round-trip. When `memory` is absent on the request, the persisted JSON omits the key entirely (no default populated). When `memory` is present, preserve the three fields verbatim, including `null`-valued `summarizerModel` or `maxEntries`.

This is the step most likely to silently drop the new field — verify round-trip by reading back the persisted `agent_config` and deserialising it into `AgentConfigRequest`.

### Consumer expectations

This task is a PURE config surface task. Do not:

- Register memory tools on the worker.
- Add a `memory_write` node to any graph.
- Write any row to `agent_memory_entries`.
- Change the task-submission payload or task-detail response.

All of those belong to later tasks. The only user-visible effect of this task is that `POST /v1/agents` and `PUT /v1/agents/{agent_id}` now accept and persist the `memory` sub-object.

## Acceptance Criteria

- [ ] `POST /v1/agents` with `agent_config.memory.enabled = true` and no other memory keys succeeds and the row is readable back with the `memory` sub-object intact.
- [ ] `POST /v1/agents` with `agent_config.memory.summarizer_model = "claude-haiku-4-5"` and a matching active row in `models` succeeds.
- [ ] `POST /v1/agents` with `agent_config.memory.summarizer_model = "nonexistent-model"` fails with a 400 and an error message consistent with the existing "unknown model" path.
- [ ] `POST /v1/agents` with `agent_config.memory.summarizer_model = "<valid-but-disabled-row>"` fails with a 400.
- [ ] `POST /v1/agents` with `agent_config.memory.max_entries = 10` fails with a 400 naming the 100–100000 range.
- [ ] `POST /v1/agents` with `agent_config.memory.max_entries = 500_000` fails with a 400 naming the 100–100000 range.
- [ ] `POST /v1/agents` with `agent_config.memory.max_entries = 5000` succeeds.
- [ ] `PUT /v1/agents/{agent_id}` follows the same validation rules as POST.
- [ ] Agents created without `memory` on the payload persist the config with `memory` absent (not `null`-valued, not populated with defaults). Reading back → `memory` field is `null` / absent.
- [ ] Agents created before this task (no `memory` field in their persisted JSON) are still readable and usable — no migration of existing rows is required.
- [ ] `make test` — all Java unit tests pass, including the new validation tests.

## Testing Requirements

- **Unit tests:** Validation for each of the reject cases above; canonicalisation round-trip tests showing that `memory` is preserved exactly (and is omitted when absent).
- **Regression:** existing agent-creation tests pass unchanged. No existing call site of `validateAgentConfig` is broken by the new nested sub-object.
- **No DB tests needed in this task** — the persisted JSON is just bytes in `agent_config`; Task 1 already ships the schema that holds it.

## Constraints and Guardrails

- Do not change the `agents` table schema.
- Do not write default values for `maxEntries` or `summarizerModel` into the persisted config — defaults apply at read time only. Absence must stay absent.
- Do not add Console UI for the toggle — Task 9 (Console Memory tab) includes the edit form.
- Do not add runtime gating or tool registration — that is Task 6 / Task 7.
- Do not introduce new Jackson global config (no `IGNORE_UNKNOWN_PROPERTIES=true`). The explicit typed field is the correct fix.
- Do not add memory-related fields to `TaskSubmissionRequest` here — Task 4 handles that.
- Error messages must match existing style (see how `validateModel` formats its errors); do not invent new error codes.

## Assumptions

- The `models` table row type and the provider-credential validation path already exist from Phase 1 / Track 1. Task 2 does not touch model discovery.
- The chat `model` field on the agent config is validated in the same helper — reuse that validator for `summarizer_model`.
- Jackson is already configured to use snake_case on JSON ↔ camelCase on Java records (check `application.yml` or the active `ObjectMapper` bean). If it is not, the JSON key names above must be adjusted to match the effective configuration.
- No feature flag is required — the field is opt-in by virtue of `enabled` defaulting to `false`.

<!-- AGENT_TASK_END: task-2-agent-config-extension.md -->
