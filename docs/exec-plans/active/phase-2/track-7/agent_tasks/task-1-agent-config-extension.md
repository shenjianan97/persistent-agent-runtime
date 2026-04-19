<!-- AGENT_TASK_START: task-1-agent-config-extension.md -->

# Task 1 — Agent Config Extension: `context_management` Sub-Object

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-7-context-window-management.md` — sections "Agent config extension" and "Validation and consistency rules".
2. `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` — current shape including the existing `MemoryConfigRequest memory` nested field.
3. `services/api-service/src/main/java/com/persistentagent/api/model/request/MemoryConfigRequest.java` — canonical pattern for a nested config sub-object with Jackson mapping.
4. `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` — existing `validateAgentConfig`, `validateMemoryConfig`, and `validateModel` helpers.
5. `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` — `canonicalizeConfig` round-trip semantics (no silent defaults written on absent fields).
6. Track 5 task `task-2-agent-config-extension.md` in `docs/exec-plans/active/phase-2/track-5/agent_tasks/` — same shape of change for the `memory` field; use as a pattern template.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make test` (Java + worker unit tests). Fix any regressions.
2. Update the status in `docs/exec-plans/active/phase-2/track-7/progress.md` to "Done".

## Context

Track 7 (Context Window Management) is platform infrastructure — every agent gets compaction, no per-agent opt-out. The `context_management` sub-object exposes three tuning fields: `summarizer_model` (for Tier 3), `exclude_tools` (tool results never masked by Tier 1), and `pre_tier3_memory_flush` (interaction with Track 5).

Because Spring Boot's default Jackson is configured with `FAIL_ON_UNKNOWN_PROPERTIES = true`, the `AgentConfigRequest` record MUST be extended with a typed `ContextManagementConfigRequest contextManagement` field. Without this extension, requests carrying `context_management` fail schema validation before reaching the service layer, and `AgentService.canonicalizeConfig` drops the field on round-trip.

## Task-Specific Shared Contract

- `agent_config.context_management` has three optional tuning fields — all three absent-friendly. **There is no `enabled` field.** Track 7 is platform infrastructure, not a feature with a per-agent toggle; customers cannot disable compaction on an agent because an agent without compaction simply fails once history exceeds the provider's context window.
  - `summarizer_model: string`, optional. When present, must reference an active row in `models` for the agent's provider. When absent, the worker falls back to `claude-haiku-4-5` (platform default, defined worker-side in Task 3).
  - `exclude_tools: list[string]`, optional. Default `[]`. Max 50 entries (matches `tool_servers`). Each string is a tool name; unknown names are allowed (customer tools can be added before they are wired). Platform defaults (`memory_note`, `save_memory`, `request_human_input`, `memory_search`, `task_history_get`) are always applied regardless of this field — the agent list is *additive*, not a replacement.
  - `pre_tier3_memory_flush: bool`, optional. When absent the worker treats it as `true`, matching the design default. No-op if `agent.memory.enabled=false` — validation does not enforce memory coupling; runtime skipping does.
- Canonicalisation: `AgentService.canonicalizeConfig` MUST round-trip the `context_management` sub-object exactly as stored. When the sub-object is absent on the request, the persisted JSON omits the key entirely (no default populated). When present, preserve all three fields verbatim, including `null`-valued `summarizerModel` or an empty `excludeTools` list.
- This task does not change worker runtime behavior; it only lands the API config surface. Worker behavior is wired in Task 8.
- Validation runs at both `POST /v1/agents` and `PUT /v1/agents/{agent_id}` — reuse the helper for both paths.

## Affected Component

- **Service/Module:** API Service — Agents
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/ContextManagementConfigRequest.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/request/AgentConfigRequest.java` (modify — add typed `contextManagement` field)
  - `services/api-service/src/main/java/com/persistentagent/api/service/ConfigValidationHelper.java` (modify — add `validateContextManagementConfig` invoked from `validateAgentConfig`)
  - `services/api-service/src/main/java/com/persistentagent/api/service/AgentService.java` (modify — `canonicalizeConfig` round-trip)
  - `services/api-service/src/test/java/.../AgentConfigValidationTest.java` (new or extend)
  - `services/api-service/src/test/java/.../AgentServiceCanonicalizeTest.java` (extend if present, else add)
- **Change type:** new record + modification of request model + service-layer validation

## Dependencies

- **Must complete first:** None. Task 1 is independent of Task 3 (worker constants).
- **Provides output to:** Task 8 (worker pipeline reads `summarizer_model`, `exclude_tools` from the persisted sub-object), Task 9 (reads `pre_tier3_memory_flush`), Task 11 (Console form mirrors these fields).
- **Shared interfaces/contracts:** The JSON shape of `agent_config.context_management`.

## Implementation Specification

### New record: `ContextManagementConfigRequest`

Create a Java `record` mirroring `MemoryConfigRequest` style with:

- `String summarizerModel` — nullable. Snake-case JSON key `summarizer_model`.
- `List<String> excludeTools` — nullable. Snake-case JSON key `exclude_tools`. Max size 50.
- `Boolean preTier3MemoryFlush` — nullable. Snake-case JSON key `pre_tier3_memory_flush`.

Three fields total. No `enabled` field — the sub-object is purely for tuning and carries no opt-out knob. All fields nullable so partial payloads are accepted.

**Unknown-property rejection.** Because Spring Boot's Jackson is configured with `FAIL_ON_UNKNOWN_PROPERTIES = true`, the request record MUST reject an `enabled` key if a client sends one. This is the correct behavior — it surfaces a 400 with "Unrecognized field 'enabled'" rather than silently dropping the field. No manual guard needed; the default Jackson behavior enforces it.

### Modify: `AgentConfigRequest`

Add a field:

- `ContextManagementConfigRequest contextManagement` — nullable. Snake-case JSON key `context_management`. Use `@JsonInclude(JsonInclude.Include.NON_NULL)` so serialization omits it when absent (same pattern as `memory`).

### Modify: `ConfigValidationHelper.validateAgentConfig`

Add a new helper `validateContextManagementConfig(ContextManagementConfigRequest cm, String provider)` invoked from `validateAgentConfig` when `cm != null`:

1. When `cm.summarizerModel()` is non-null and non-empty:
   - Reuse the same `validateModel(String, String provider)` helper used for the agent's primary `model` and for `memory.summarizer_model`.
   - Reject when the row is not `active`, when the provider does not match the agent's provider, or when provider credentials are not resolvable.
   - Error message consistent with existing "unknown model" / "disabled model" messages.
2. When `cm.excludeTools()` is non-null:
   - Reject when size > 50 with a message naming the 50-entry cap.
   - Do NOT validate tool-name existence — customers may add custom tools before those tools are wired.
3. No cross-field validation — `pre_tier3_memory_flush=true` is valid even if `memory.enabled=false`. Runtime gating is the worker's job.

Do NOT write defaults into the canonical config (see next section). Validation either accepts absence or rejects an explicit out-of-range / unresolvable value.

### Modify: `AgentService.canonicalizeConfig`

Add the `context_management` sub-object to the round-trip identically to how `memory` is handled:

- When `contextManagement` is absent on the request, the persisted JSON omits the key entirely.
- When present, preserve all three fields verbatim, including `null`-valued `summarizerModel` or an empty `excludeTools` list.
- Verify round-trip by reading the persisted `agent_config` and deserialising it back into `AgentConfigRequest`.

### Consumer expectations

This task is a PURE config surface task. Do not:

- Register any compaction behavior on the worker.
- Add compaction state fields anywhere.
- Change the task-submission payload or task-detail response.
- Add Console UI (Task 11).

All of those belong to later tasks. The only user-visible effect of this task is that `POST /v1/agents` and `PUT /v1/agents/{agent_id}` now accept and persist the `context_management` sub-object.

## Acceptance Criteria

- [ ] `POST /v1/agents` with `agent_config.context_management = {}` (empty sub-object) succeeds; the row reads back with an empty sub-object.
- [ ] `POST /v1/agents` with `agent_config.context_management.enabled = true` fails with a 400 "Unrecognized field 'enabled'" — Track 7 has no enabled toggle.
- [ ] `POST /v1/agents` with `agent_config.context_management.summarizer_model = "claude-haiku-4-5"` and a matching active row in `models` succeeds.
- [ ] `POST /v1/agents` with `agent_config.context_management.summarizer_model = "nonexistent-model"` fails with a 400 and an error message consistent with the existing "unknown model" path.
- [ ] `POST /v1/agents` with `agent_config.context_management.summarizer_model = "<valid-but-disabled-row>"` fails with a 400.
- [ ] `POST /v1/agents` with `agent_config.context_management.exclude_tools` of size 51 fails with a 400 naming the 50-entry cap.
- [ ] `POST /v1/agents` with `agent_config.context_management.exclude_tools` of size 50 succeeds.
- [ ] `POST /v1/agents` with `agent_config.context_management.exclude_tools = ["memory_note", "unknown_tool"]` succeeds (unknown tool names are allowed).
- [ ] `POST /v1/agents` with `agent_config.context_management.pre_tier3_memory_flush = true` AND `agent_config.memory.enabled = false` succeeds — no cross-field validation.
- [ ] `PUT /v1/agents/{agent_id}` follows the same validation rules as POST.
- [ ] Agents created without `context_management` on the payload persist the config with `context_management` absent (not `null`-valued, not populated with defaults). Reading back → `context_management` field is `null` / absent.
- [ ] Agents created before this task (no `context_management` field in their persisted JSON) are still readable and usable — no migration of existing rows is required.
- [ ] `make test` — all Java unit tests pass, including the new validation tests.

## Testing Requirements

- **Unit tests:** Validation for each reject case above; canonicalisation round-trip tests showing that `context_management` is preserved exactly and is omitted when absent.
- **Regression:** existing agent-creation tests pass unchanged. No existing call site of `validateAgentConfig` is broken by the new nested sub-object.
- **No DB tests needed in this task** — the persisted JSON is just bytes in `agent_config`; no schema change.

## Constraints and Guardrails

- Do not change the `agents` table schema.
- Do not write default values into the persisted config — defaults apply at read time only. Absence must stay absent.
- Do not add Console UI — Task 11 covers that.
- Do not add runtime gating or pipeline invocation — that is Task 8.
- Do not introduce new Jackson global config (no `IGNORE_UNKNOWN_PROPERTIES=true`). The explicit typed field is the correct fix.
- Do not add context-management fields to `TaskSubmissionRequest` — Track 7 has no per-task knobs in v1.
- Error messages must match existing style. Do not invent new error codes.
- Do not cross-validate `pre_tier3_memory_flush` against `memory.enabled` — runtime gating is the worker's job (Task 9).

## Assumptions

- The `models` table row type and the provider-credential validation path exist from Phase 1 / Track 1.
- The agent's primary `model` field is validated by an existing helper — reuse for `summarizer_model`.
- Jackson is configured for snake_case JSON ↔ camelCase Java records (already in place from Track 5).
- No feature flag required — the field is opt-in-by-absence (missing sub-object = platform defaults = `enabled=true` at runtime).

<!-- AGENT_TASK_END: task-1-agent-config-extension.md -->
