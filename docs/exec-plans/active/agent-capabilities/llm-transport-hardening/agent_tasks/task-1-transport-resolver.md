<!-- AGENT_TASK_START: task-1-transport-resolver.md -->

# Task 1 — Transport Defaults + Resolver

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — the overall architecture and how this task fits.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — symptom and root cause.
3. `services/worker-service/executor/providers.py` — current LLM construction; this task creates the resolver that the rewritten `providers.py` (Task 2) will consume.
4. `services/worker-service/core/agent_repository.py` (or wherever `agent_config` is loaded into a Python dict) to understand the input shape Task 7 will produce.

**CRITICAL POST-WORK:** After completing this task:
1. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_transport_resolver.py -v`. Fix regressions.
2. Update `progress.md` for this row to "Done".

## Context

The runtime needs a single source of truth for "how should we configure the LLM transport for this call." Today the answer is hard-coded in two places (`providers.py` for timeout, nowhere for max_tokens), and per-agent overrides do not exist. This task introduces a small pure module that returns `(connect_timeout_s, read_timeout_s, max_output_tokens)` given an agent's config and the resolved model. Tasks 2 / 3 / 4 / 7 all consume this module; isolating it now lets them proceed in parallel.

## Task-Specific Shared Contract

- **Defaults (platform-owned, not customer-tunable globally):**
  - `connect_timeout_s = 10`
  - `read_timeout_s = 120` (per-read; with streaming this is per-chunk inactivity)
  - `max_output_tokens = 16_384`
- **Per-agent overrides** (all optional, all from `agent_config.llm_transport`, shipped by Task 7):
  - `connect_timeout_s ∈ [1, 60]`
  - `read_timeout_s ∈ [10, 900]`
  - `max_output_tokens ∈ [256, 200_000]`
- **Resolver returns a frozen dataclass** so callers cannot mutate the result. Out-of-bounds overrides should never reach the resolver (API-side validation in Task 7), but the resolver still defends with bounds-clamping + a `WARN` log so a bad row in the DB doesn't crash the worker.
- **Behavior on absent sub-object:** return defaults verbatim, no warning.
- **Provider-aware:** the signature accepts the resolved provider (`bedrock` / `openai` / `anthropic`) but the current implementation is provider-agnostic. The argument exists so future per-provider tuning has a place to live without a signature break.

## Affected Component

- **Service/Module:** Worker — Executor
- **File paths:**
  - `services/worker-service/executor/transport.py` (new)
  - `services/worker-service/tests/test_transport_resolver.py` (new)
- **Change type:** new module + tests

## Dependencies

- **Must complete first:** None.
- **Provides output to:** Tasks 2, 3, 4 (worker construction + streaming), Task 7 (defines the agent-config shape this task validates against).
- **Shared interfaces/contracts:** `LLMTransportConfig` dataclass; `resolve_transport(agent_config: dict, *, provider: str, model: str) -> LLMTransportConfig` function.

## Implementation Specification

### New module: `executor/transport.py`

Export:

- `@dataclass(frozen=True) class LLMTransportConfig` with fields `connect_timeout_s: float`, `read_timeout_s: float`, `max_output_tokens: int`.
- Module-level constants `DEFAULT_CONNECT_TIMEOUT_S = 10`, `DEFAULT_READ_TIMEOUT_S = 120`, `DEFAULT_MAX_OUTPUT_TOKENS = 16_384`.
- Module-level constants for the validation bounds (so Task 7's Java side can mirror them by reference, not by guess; the bounds also live in API-side validation).
- `def resolve_transport(agent_config: Mapping[str, Any] | None, *, provider: str, model: str) -> LLMTransportConfig` returning the merged config. Behavior:
  - When `agent_config is None` or `agent_config.get("llm_transport")` is `None`, return defaults.
  - For each of the three fields: when present and within bounds, use the override; when present but out of bounds, clamp to the nearest bound and emit `logging.warning("transport.override_clamped", ...)` with `task_id=None, field, original_value, clamped_to`.
  - Provider/model arguments are accepted but unused in v1. Document this in the docstring.

### Tests: `tests/test_transport_resolver.py`

Cover:

- Defaults returned when agent_config is `None`, `{}`, or `{"llm_transport": None}`.
- Each override applied correctly when in-range.
- Each override clamped + warning emitted when out of range (low and high). Use `caplog` to assert the warning structure.
- Frozen dataclass: assigning to a returned instance raises `dataclasses.FrozenInstanceError`.
- Unknown keys inside `llm_transport` are ignored silently (forward-compat with Task 7 future fields).

## Acceptance Criteria

- [ ] Resolver returns the documented defaults when `agent_config` lacks `llm_transport`.
- [ ] Each override field is clamped to its documented bounds when out of range, with a structured `WARN` log.
- [ ] Returned `LLMTransportConfig` is frozen.
- [ ] Test file passes via `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_transport_resolver.py -v`.
- [ ] Module has no dependencies outside the Python stdlib (no asyncpg, no langchain) — keeps it trivially unit-testable.

## Out of Scope

- Wiring the resolver into `providers.py` (Task 2 / 3).
- API-side validation (Task 7).
- Persisting the override to the agents table (Task 7).

<!-- AGENT_TASK_END -->
