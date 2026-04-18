<!-- AGENT_TASK_START: task-7-worker-memory-tools.md -->

# Task 7 — Worker Memory Tools: `memory_note`, `memory_search`, `task_history_get`

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — sections "Agent Tools" end-to-end, including the tool-scope-binding contract in the section header.
2. `services/worker-service/tools/definitions.py` — how built-in tools are registered and the `ToolDependencies` shape.
3. `services/worker-service/tools/calculator.py` and `tools/read_url.py` — the canonical built-in-tool examples; new tools follow the same pattern.
4. Task 6's output — the `MemoryEnabledState` schema (`observations` is the state field `memory_note` mutates) and the `effective_memory_enabled` gate.
5. Task 3's output — the Memory REST API the `memory_search` tool delegates to.
6. The Track 4 precedent for namespacing: built-in tools like `memory_search` keep the unqualified name; only custom BYOT tools use `server_name__tool_name`.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test` and `make e2e-test`. Fix any regressions.
2. Verify memory-disabled agents still do not see `memory_note` / `memory_search` in their tool list.
3. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

Three tools. Two are memory-gated:

- `memory_note(text: string)` — append an observation to the current task's draft memory entry. Zero cost (no LLM, no network). Mutates graph state via `Command(update={"observations": [note]})` (the `operator.add` reducer from Task 6 merges appends).
- `memory_search(query: string, limit: int = 5, mode: str = "hybrid")` — delegates to the Memory REST search endpoint (Task 3) scoped to the task's tenant + agent.

One is always available regardless of memory state:

- `task_history_get(task_id: string)` — bounded structured view of a past task. Intended as a drill-down from a `memory_search` hit, but also useful as a diagnostic tool on its own.

**Scope binding is the critical contract across all three:** filters come from the worker's task context at registration time, never from LLM-supplied arguments. A compromised or confused agent can narrow within its scope (different query, different task id inside its own agent scope) but cannot broaden it. The SQL / HTTP request the tools build always includes `tenant_id = :bound AND agent_id = :bound`, appended server-side by the tool implementation.

## Task-Specific Shared Contract

- **Registration gating:**
  - `memory_note` and `memory_search` are registered ONLY when `effective_memory_enabled` (from Task 6) is `true`.
  - `task_history_get` is registered for EVERY task — memory-disabled agents get it too.
- **`ToolDependencies` assembly (common path):** the container that holds `tenant_id`, `agent_id`, `task_id`, asyncpg pool, and Memory-API HTTP client is assembled on the COMMON `execute_task` path — before any memory-gated branch. This is what lets `task_history_get` be registered for memory-disabled tasks using identical scope-binding values as memory-enabled tasks. Do NOT move scope-binding into the memory-enabled branch added by Task 6; Task 6's state-schema / graph-topology changes are independent of scope binding.
- **Scope binding:** every tool captures `tenant_id` and `agent_id` from the task-context closure at registration. LLM arguments cannot override these — the tool implementation appends them server-side (for `memory_search`, via the path parameter + authenticated context; for `task_history_get`, via the WHERE clause).
- **`memory_note(text)`:**
  - Arguments: `text` (string, 1–2048 chars). Reject empty or > 2048 chars with a usable tool error (message tells the agent what the limit is; the graph stays in-loop).
  - Returns: `{"ok": true, "count": <current observation count>}`. Cost: zero.
  - Side effect: returns `Command(update={"observations": [text]})` — relies on `operator.add` reducer in `MemoryEnabledState`.
- **`memory_search(query, limit=5, mode="hybrid")`:**
  - Arguments: `query` (non-empty string), `limit` (default 5, max 10 as a TOOL limit — the REST API allows up to 20 but keep the tool footprint small), `mode` (`hybrid`\|`text`\|`vector`).
  - Returns: list of `{memory_id, title, summary_preview, outcome, task_id, created_at, score}` objects.
  - Delegates to the internal Memory API. When the API returns a tool-recoverable error (e.g., embedding down + `mode=vector`), surface as a tool error so the agent can retry with `mode=text`. This is the "tool-surface equivalent of the 503" the design doc references.
  - Cost: one embedding call if mode uses vector (billed server-side, not on the worker side).
- **`task_history_get(task_id)`:**
  - Arguments: `task_id` (string UUID).
  - Returns: `{task_id, agent_id, input, status, final_output, tool_calls: [{name, args_preview, result_preview}], error_code, error_message, created_at, memory_id}`. Fields are **bounded** — input and final_output truncated at ~2 KB each; tool_calls capped at 20 items; args_preview / result_preview truncated.
  - Full raw message transcripts are NOT returned.
  - Scope enforcement: the SQL read includes `tenant_id = :bound AND agent_id = :bound`. Cross-agent or cross-tenant task ids return a tool error shaped "not found" — consistent with the 404-not-403 rule.
- **No tool returns expose credentials or memory contents from other agents.** The repository layer for `task_history_get` must refuse to execute a query without the scope predicate (same rule as Task 3's Memory API).
- **Namespacing:** built-in unqualified names (`memory_note`, `memory_search`, `task_history_get`). Do NOT apply the BYOT `server__tool` prefix.
- **Registration:** expose a helper (e.g., `build_memory_tools(deps: ToolDependencies, effective_memory_enabled: bool) -> list[StructuredTool]`) invoked from `graph.py` during graph assembly. Keep existing tool-wiring structure intact.

## Affected Component

- **Service/Module:** Worker Service — Tools + Executor registration
- **File paths:**
  - `services/worker-service/tools/memory_tools.py` (new — `memory_note`, `memory_search`, `task_history_get`, `build_memory_tools` helper)
  - `services/worker-service/tools/definitions.py` (modify — register via the new `build_memory_tools` path; extend `ToolDependencies` if needed to carry the Memory API base URL / HTTP client / asyncpg pool for `task_history_get`)
  - `services/worker-service/executor/graph.py` (modify — call the registration helper with `effective_memory_enabled`, pass task-context for scope binding)
  - `services/worker-service/tests/test_memory_tools.py` (new)
- **Change type:** new tools + modification to registration path

## Dependencies

- **Must complete first:** Task 3 (Memory REST API — `memory_search` delegates here), Task 6 (state schema + `effective_memory_enabled` gate + graph topology).
- **Provides output to:** Task 8 (dead-letter hook uses observations from state, seeded by `memory_note`), Task 11 (E2E).
- **Shared interfaces/contracts:** The three tool names, their argument shapes, and their scope-binding invariant.
- **Parallel-safety:** Tasks 6 and 8 both edit `services/worker-service/executor/graph.py`. If dispatched concurrently, use `isolation: "worktree"` on one or more agents and merge on completion per AGENTS.md §Parallel Subagent Safety.

## Implementation Specification

### `memory_note` contract

- Implement using LangChain's `StructuredTool` with an `args_schema` Pydantic model enforcing `text` length 1–2048.
- The tool function returns `Command(update={"observations": [text]})`. The graph sees the state update after the super-step commits.
- Emit no log line per call (too noisy). At DEBUG, log `memory.note.appended` with `count_after_append`.
- No DB write, no HTTP call.

### `memory_search` contract

- Use an internal HTTP call to the Memory API (Task 3) — `GET /v1/agents/{bound_agent_id}/memory/search`. The URL's `agent_id` comes from the bound task context; the caller cannot override it.
- Authenticate as the worker (existing inter-service auth pattern — if none exists, add an inter-service token env var and document it).
- Propagate the tool's `mode` argument to the query string. If the API returns 503 on `mode=vector`, translate to a tool error whose message tells the agent to try `mode=text`.
- Bound `limit` to 10 at the tool layer (the REST API caps at 20; keep the tool's result token footprint small).
- Return the API's JSON `results` list verbatim. **Pass through `ranking_used`** so the agent learns when its `mode=hybrid` request silently degraded to `text` (e.g., embedding provider down). Returning the field costs a handful of tokens and lets the agent decide whether to retry or continue.

### `task_history_get` contract

- Query `tasks` directly using the worker's asyncpg pool (no Memory API round-trip needed). SQL uses `WHERE task_id = :id AND tenant_id = :bound AND agent_id = :bound`.
- Join with `agent_memory_entries` on `task_id` to surface `memory_id` when present (LEFT JOIN so non-memory tasks still resolve).
- Truncate `input` and `final_output` at 2048 bytes each. For `tool_calls`, read from the task's checkpoint / event history — pick whichever is simpler. Cap at 20 items; each item's `args_preview` and `result_preview` truncated at 512 bytes.
- Missing task id OR scope miss → return a tool error with a generic message ("not found").
- Emit `memory.task_history.served` structured log (tenant, agent, task) on success; `memory.task_history.missed` on scope miss.

### Registration helper

`build_memory_tools(deps: ToolDependencies, effective_memory_enabled: bool) -> list[StructuredTool]` returns:

- If `effective_memory_enabled`: `[memory_note_tool, memory_search_tool, task_history_get_tool]`.
- Else: `[task_history_get_tool]`.

`ToolDependencies` must carry whatever the tools need: `tenant_id`, `agent_id`, `task_id` (for context binding), `memory_api_base_url` + HTTP client, asyncpg pool, cancellation event (for the `_await_or_cancel` wrap the MCP integration already uses — reuse the same pattern for the HTTP search call).

### Graph.py integration

- In `_build_graph` / `execute_task`, compute `effective_memory_enabled` (from Task 6), assemble `ToolDependencies`, call `build_memory_tools`, and append the result to the regular built-in tools list BEFORE merging with BYOT custom tools.
- Respect the existing `MAX_TOOLS_PER_AGENT = 128` cap. The three memory tools count toward this cap.

## Acceptance Criteria

- [ ] For a memory-enabled agent + task, all three tools are in the graph's tool list.
- [ ] For a memory-disabled agent OR a task with `skip_memory_write=true`, the list contains `task_history_get` only.
- [ ] `memory_note("hello")` returns `{"ok": true, "count": 1}` and the state's `observations` contains `["hello"]` after the super-step commits.
- [ ] `memory_note("")` and `memory_note("a" * 2049)` return a tool error (not a crash); `observations` is unchanged.
- [ ] `memory_search(query="x", mode="hybrid")` returns a list of hit objects from the Memory API for the task's agent.
- [ ] `memory_search` scope is immutable — calling it with any argument that could hypothetically override scope (e.g., a crafted URL-like string in `query`) does NOT broaden the result set to another agent.
- [ ] `memory_search(mode="vector")` surfaces a tool error with a recoverable message when the embedding provider is down.
- [ ] `task_history_get(task_id=<same-agent-task>)` returns the bounded structure with correct truncation.
- [ ] `task_history_get(task_id=<another-agent-task-same-tenant>)` returns a tool-shaped "not found" error; the graph stays in-loop.
- [ ] `task_history_get(task_id=<another-tenant-task>)` returns the same tool-shaped "not found".
- [ ] `task_history_get` is available even when `effective_memory_enabled` is false.
- [ ] Total tools still cap at 128; the three new tools count toward the cap.
- [ ] `make worker-test` passes; the full test suite still passes.

## Testing Requirements

- **Unit tests** for each tool:
  - `memory_note` argument validation + state-update shape (mock out the `Command` consumer).
  - `memory_search` delegates to the Memory API with the bound agent id in the URL; `mode=vector` with 503 → tool error; normal hybrid response → result list.
  - `task_history_get` bounded fields, truncation, scope miss.
- **Executor integration tests:** memory-enabled agent receives the three tools; memory-disabled agent receives only `task_history_get`; `skip_memory_write=true` acts like memory-disabled for registration.
- **Scope-binding test:** construct a task-context with agent A; call `memory_search` and assert the HTTP request URL contains agent A; construct a `task_history_get` call with a task id for agent B → tool error.
- **Regression:** existing executor and tool-registration tests pass.

## Constraints and Guardrails

- Do not add tool configuration to the agent config (no `allowed_memory_tools`); all three are registered together.
- Do not expose `tags` as a tool argument on `memory_note` — v1 has no agent-facing tag API.
- Do not let LLM arguments reach the SQL or URL path of any tool without the scope predicate appended server-side.
- Do not log tool argument values at INFO — they may carry PII. DEBUG only.
- Do not cache search results in the worker process.
- Do not attempt to short-circuit `memory_search` by reading `agent_memory_entries` directly from the worker — delegate to the Memory API (Task 3) so ranking / filtering stays in one place.
- Do not use scope-broadening patterns like "if agent_id argument provided, use it" — scope is always bound from task context.
- Do not expose the user's raw message history via `task_history_get` — only the bounded preview. Raw-trace access is Console / status-endpoint territory.

## Assumptions

- Task 3 has shipped — the Memory API is reachable from the worker. The worker-to-API network path exists (same pod / same VPC / same docker-compose network).
- Task 6 has shipped — `MemoryEnabledState` with `observations` reducer + `effective_memory_enabled` gate + graph topology are in place. `memory_note` can return a `Command(update=…)` that the reducer merges.
- Inter-service auth between the worker and the API either already exists (reuse) or can be added with a dedicated env var (document it; do not commit a secret).
- `ToolDependencies` or an equivalent registration container exists; extending it with the new fields is low-risk.
- The existing `_await_or_cancel` pattern in `graph.py` is usable for the HTTP call; reuse it for cancellation safety.

<!-- AGENT_TASK_END: task-7-worker-memory-tools.md -->
