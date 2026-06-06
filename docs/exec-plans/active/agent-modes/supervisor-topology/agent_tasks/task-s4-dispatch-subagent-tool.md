<!-- AGENT_TASK_START: task-s4-dispatch-subagent-tool.md -->

# Task S4 — `dispatch_subagent` Built-In Tool (ReAct Topology 1)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"Delegation tools available in ReAct"** (the `dispatch_subagent(prompt, tools, budget)` subsection — the `budget` arg is the per-sub-agent ceiling, *load-bearing not cosmetic*), **"Shared fan-out machinery"** (the ReAct/`dispatch_subagent` column of the drivers table **and** the *"Why `dispatch_subagent` routes through `Send` rather than the ToolNode"* paragraph — `dispatch_subagent` is **NOT** executed inside the ToolNode; a **post-agent routing edge** intercepts the LLM's tool call and `Send`s it to the shared subagent node, which threads its summary back as a `ToolMessage` keyed to the `tool_call_id` while internal messages stay on a separate channel), and **"Execution model: in-process fan-out (Pattern A)"** (esp. the **Wiring requirement** — both drivers route through `Send` to the same checkpointed subagent node).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — **§A0 invariant 1** (Pattern A only), **§A0 invariant 7** (depth cap 2; ReAct-only sub-agents), **§A4.1 rows S3 and S4** (S4's contract: "`dispatch_subagent(prompt, tools, budget)` built-in tool; the LLM emits the call, a **post-agent routing edge intercepts it and `Send`s it to the shared subagent node** — **not** executed inside the ToolNode — inheriting the same per-inner-turn durability as the Supervisor; the subagent node threads its summary back as a `ToolMessage` keyed to the original `tool_call_id`, inner messages on the separate channel; allowlist-gated; depth/budget from graph state; mixed turns split — dispatch → `Send`, others → ToolNode, every `tool_call_id` gets exactly one `ToolMessage` before the next LLM call"), **§A10 decisions 3 & 4** (one shared subagent node, both drivers route via `Send`; Pattern A with per-inner-turn durability), **§A5** (the "`dispatch_subagent` tool / Supervisor `Send` → `run_subagent`" integration row — ceiling/timeout exhaustion returns a structured failure marker), and **§A9** ("Build the shared helper (S3) once … two *drivers* over the same `run_subagent`. Don't fork the machinery").
3. The completed **Task S3** spec (`task-s3-shared-fanout-helper.md`) and its delivered `executor/subagents/fanout.py` — you are *wrapping* `run_subagent`; read its `SubagentResult` success/failure shapes and `MAX_SUBAGENT_DEPTH`.
4. `services/worker-service/executor/graph.py` — the tool-registration surface you extend: `_get_tools` (`:850-1057`), the `MAX_TOOLS_PER_AGENT` cap (`:1262-1264`), `llm.bind_tools` (`:1269`), and especially the **memory-tool registration as the pattern** — `build_memory_tools(...)` is invoked at `:1233`, its results wrapped and appended to `tools` at `:1238` (`tools = tools + [_wrap_tool_with_cap(t) for t in raw_memory_tools]`). Mirror this allowlist-gated, closure-bound registration shape.
5. `services/worker-service/tools/definitions.py` and `tools/memory_tools.py` — canonical built-in tool definition style (a structured tool with a typed args schema, closure-bound over runtime context). `tools/definitions.py:449` (the `interrupt()` tool) is a reference for a built-in tool that reaches into graph runtime.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests with the pinned venv — e.g. `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_dispatch_subagent_tool.py -q`, or via the isolated harness `make e2e-test PYTEST_ARGS='-k dispatch_subagent'`. **Never** run raw `pytest tests/backend-integration` in a worktree. Fix any regression before moving on.
2. Update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S4 status and note it is the Topology-1 driver over S3's `run_subagent`.

## Context

`dispatch_subagent` is the **LLM-emergent** driver over the shared fan-out helper (Task S3's `run_subagent`). In a ReAct loop the LLM emits a `dispatch_subagent` tool call when it wants to delegate a focused subtask without polluting its own context (e.g. "investigate why test X is flaky"). **The tool call is NOT executed inside the ToolNode.** Instead, a **post-agent routing edge** inspects the agent's emitted tool calls and, for each `dispatch_subagent` call, returns a `Send("subagent", {...})` to the **shared subagent node** (S3) — exactly the structural mechanism the Supervisor uses — so it inherits the same **per-inner-turn crash-resume durability**. The subagent node threads its summary back as a **`ToolMessage` carrying the original `tool_call_id`**, while the sub-agent's internal working messages stay on a **separate channel** (context isolation preserved — the parent sees only the summary). The Supervisor topology (Task S6) is the *other* driver — structural `Send` fan-out — over the **same** subagent node / `run_subagent`. This task must not fork the machinery: it is the LLM-facing tool definition **plus the routing-edge wiring** that turns its tool call into a `Send` — **not** a plain tool-function wrapper that runs the sub-agent inside the ToolNode.

The tool is opt-in via the agent's tool allowlist. Per the design's preset table, the presets that enable it are **`coding`** and **`investigation`** (both ReAct, both delegate focused subtasks). `chat` does not enable it; `research` is Supervisor topology (structural fan-out, no `dispatch_subagent` needed).

## Task-Specific Shared Contract

- **Tool signature:** `dispatch_subagent(prompt, tools, budget)` — the LLM-facing tool. `prompt` is the subtask instruction; `tools` is the sub-agent's tool allowlist (a subset the parent may delegate); `budget` is the **per-sub-agent ceiling** (token + turn cap) carried into the shared subagent node as `run_subagent`'s `ceiling`.
- **Routed through `Send`, NOT executed in the ToolNode.** The tool itself only declares the LLM-facing schema; the actual delegation is performed by a **post-agent routing edge** that intercepts each emitted `dispatch_subagent` tool call and returns `Send("subagent", {prompt, tools, ceiling=budget, depth=<from graph state>, tool_call_id, ...})` to the **shared subagent node** (S3) — the same structural mechanism the Supervisor uses, so it inherits per-inner-turn durability. **No new fan-out machinery** — the routing edge reuses S3's subagent node; it does not re-implement the ReAct loop or run it inside the ToolNode.
- **`depth` comes from graph state**, not from the LLM. The tool reads the current `depth` from the parent's runtime state and passes it to `run_subagent` (which increments and enforces `MAX_SUBAGENT_DEPTH = 2`). The LLM cannot set or escalate depth.
- **`tools` is subject to the headless filter.** The LLM-supplied `tools` arg flows into `run_subagent`, which filters out any `interrupt()`-bearing tool (`request_human_input` and any future pause tool) before binding — sub-agents are headless (§A0 inv. 8, §A11-E3). The LLM **cannot smuggle an interrupt tool into a sub-agent**: even if it lists `request_human_input` in `tools`, the helper drops it. (Enforcement lives in S3; S4 relies on it and must not bypass it.)
- **Result threads back as a `ToolMessage` keyed to the original `tool_call_id`.** The subagent node produces a **`ToolMessage` carrying the dispatch call's `tool_call_id`**, appended to the parent's message history (so the provider sees the tool call answered). Success → the structured summary is the `ToolMessage` content. Failure markers (`ceiling | timeout | error | depth`) → a `ToolMessage` describing the failure, **not** a raised graph error — the parent LLM sees the failure and can react (retry differently, proceed, or stop). The sub-agent's **internal working messages stay on the separate channel** (isolation preserved — the parent sees only the summary `ToolMessage`). This mirrors how `run_subagent` returns rather than raises.
- **Mixed-turn handling — split dispatch vs. normal tool calls.** When the LLM emits a `dispatch_subagent` call **alongside** normal tool calls in one turn, the routing edge **splits** them: `dispatch_subagent` calls → `Send` to the subagent node; all other tool calls → the ToolNode in the same super-step. **Every** `tool_call_id` in the turn must receive **exactly one** `ToolMessage` before the next LLM call — an unanswered tool call makes the provider API error. So dispatch results (`ToolMessage` keyed to their `tool_call_id`) and ToolNode results must both land before the agent node runs again.
- **Allowlist-gated registration.** Register the tool in `_get_tools` **only when** the agent's resolved tool allowlist includes `dispatch_subagent`, following the `build_memory_tools` closure-bound pattern (`graph.py:1233-1238`). Closure-bind it over the runtime context it needs (model handle, identifiers, event-emit callable, current `depth`) so the LLM cannot broaden scope. (Gating controls only whether the LLM *sees* the tool; the actual delegation is the routing-edge `Send`, below — not a tool-function body.)
- **Touches graph wiring, not just registration.** Because the delegation is a **post-agent routing edge / conditional edge** (intercepting the tool call → `Send` to the subagent node), this task modifies `executor/graph.py`'s **graph construction** (the post-agent routing / conditional edges feeding the subagent node and ToolNode), not only `_get_tools` tool registration. The §A3 / §A9 **worktree-safety note on `graph.py`** therefore applies in full.
- **Respect `MAX_TOOLS_PER_AGENT`.** The registration must pass through (or count against) the existing tool-count cap (`graph.py:1262`) exactly like every other built-in tool — no special exemption.
- **Pattern A.** The sub-agent runs in-process; no new task row, lease, `parent_task_id`, `sub_agent_id`, or `waiting_for_subagent`. Its cost rolls into the parent's super-step (handled inside `run_subagent`).

## Affected Component

- **Service/Module:** Worker Service — Agent Modes / delegation tools
- **File paths:**
  - `services/worker-service/tools/subagent_tools.py` (new — `dispatch_subagent` tool definition + its typed args schema; a `build_dispatch_subagent_tool(...)`-style factory mirroring `build_memory_tools`), **or** an additive block in `services/worker-service/tools/definitions.py` if that better matches the existing built-in style. Prefer a new file to keep the surface isolated.
  - `services/worker-service/executor/graph.py` (modify — **two** changes: (1) register the tool in `_get_tools` (`:850-1057`) when allowlisted, mirroring the `build_memory_tools` block at `:1233-1238`, respecting `MAX_TOOLS_PER_AGENT` at `:1262`; **(2) graph wiring** — add the **post-agent routing edge / conditional edges** that intercept a `dispatch_subagent` tool call and `Send` it to the shared subagent node (S3) while routing normal tool calls to the ToolNode. This is a **graph-construction** change, not just registration; the §A3/§A9 **worktree-safety note on `graph.py` applies**.)
  - `services/worker-service/tests/test_dispatch_subagent_tool.py` (new — fake-model unit tests; **worktree-concurrency-safe**, no bound ports)
- **Change type:** new tool definition + `_get_tools` registration + **post-agent routing-edge graph wiring** in `graph.py`

## Dependencies

- **Must complete first:** **S3** (`executor/subagents/fanout.py::run_subagent`) — this tool wraps it and is meaningless without it. **S3 blocks S4.**
- **Provides output to:** S11 (Supervisor/ReAct integration + E2E exercises the tool path end-to-end). Independent of S5–S7 (the Supervisor graph is the *other* driver; both depend on S3, not on each other).
- **Shared-file note:** S4 edits `executor/graph.py::_get_tools`, which the Planning Primitive track's P1 (`plan_write`) and S3 (if it extended `state.py`) also touch. **If working in parallel with another agent that edits `graph.py`, use `isolation: "worktree"` and merge after** (§A3, §A9 worktree-safety).

## Implementation Specification

### New tool: `dispatch_subagent`

1. Define a built-in structured tool `dispatch_subagent` with a typed args schema exposing `prompt`, `tools`, and `budget` to the LLM. Model the definition on the existing memory tools (`tools/memory_tools.py` via `build_memory_tools`) — a factory that closure-binds runtime context and returns the tool. **This tool is the LLM-facing schema only** — its presence lets the LLM *emit* the call; it is **not** a tool-function that runs the sub-agent inside the ToolNode (the delegation is the routing edge, below).
2. Delegation is performed by the **post-agent routing edge**, not the tool body:
   - After the agent node, the routing edge inspects the emitted tool calls. For each `dispatch_subagent` call it returns `Send("subagent", {...})` to the **shared subagent node** (S3), passing `prompt`, the (filtered-downstream) `tools`, `ceiling=budget`, `depth=<from parent graph state>`, the original `tool_call_id`, and the same model handle / identifiers / event-emit callable the parent uses. `depth` is read from graph state, never from the LLM args.
   - The subagent node runs `run_subagent` and threads its result back as a **`ToolMessage` carrying the original `tool_call_id`**: success → the structured summary as content; failure marker (`ceiling | timeout | error | depth`) → a descriptive `ToolMessage`, never a raised graph error. The sub-agent's internal messages stay on the **separate channel**.
   - **Normal (non-`dispatch_subagent`) tool calls** in the same turn still route to the **ToolNode**. **Every** `tool_call_id` emitted in the turn must get **exactly one** `ToolMessage` before the agent node runs again (unanswered tool calls → provider API error).
3. The `tools` the sub-agent may use are bounded by what the parent is allowed to delegate; do not let `dispatch_subagent` grant the sub-agent tools the parent itself lacks. (The headless `interrupt()`-tool filter is enforced in S3's `run_subagent`; S4 must not bypass it.)

### Modify: `_get_tools` registration

Mirror the `build_memory_tools` block (`graph.py:1233-1238`):
- When the resolved allowlist includes `dispatch_subagent`, build the closure-bound tool and append it to `tools` (running it through the same cap-wrap path the other built-ins use, unless that wrapper is a documented no-op).
- The registration must respect `MAX_TOOLS_PER_AGENT` (`:1262`) — it counts like any other tool; do not bypass the cap.
- When `dispatch_subagent` is **not** in the allowlist, the tool is **not** registered (no-op) — `chat`-preset and other non-delegating agents never see it.

### Modify: `executor/graph.py` graph wiring (post-agent routing edge)

This is the part that is **not** present in the plain memory-tool pattern (memory tools execute in the ToolNode; `dispatch_subagent` does not):
- Add the **post-agent routing edge / conditional edges** that, after the agent node emits tool calls, **split** them: each `dispatch_subagent` call → `Send` to the shared subagent node (S3); all other tool calls → the ToolNode — both in the same super-step, both producing a `ToolMessage` (the subagent node's keyed to the original `tool_call_id`) before the next agent-node call.

### Proven recipe — spike-verified end-to-end (spike #4, 2026-06-05; build it this way)

This exact mechanism was validated against `langgraph==1.0.5` (single dispatch, mixed turn, isolation, and a mid-sub-agent crash). Follow the proven shape:
- **Channel design for isolation.** The shared subagent subgraph carries **two** message channels: an **internal working channel** (e.g. `work` / `sub_messages`, `Annotated[..., add_messages]`) that is **NOT a channel in the parent's state schema**, and the parent-shared **`messages`** channel used **only** to emit the single summary `ToolMessage`. Because the internal channel isn't in the parent schema, the sub-agent's turns are **dropped on merge** (never reach the parent) while still being checkpointed under the sub-checkpoint namespace; only the `ToolMessage` written to `messages` crosses back. *(Spike confirmed: a sub-agent that did 2 internal turns surfaced only its summary in the parent history.)*
- **The `Send` payload is the sub-agent's entire input** (`{tool_call_id, prompt, ...}`) — the parent's `messages` are **not** passed in, so isolation holds at entry too.
- **The conditional edge may return a mixed list** of `Send("subagent", ...)` objects **and** the `"tools"` node name; LangGraph runs both in the same super-step. *(Spike confirmed mixed-turn: `dispatch_subagent` + a normal tool call both answered, no provider "unanswered tool_call" error.)*
- **Per-inner-step durability holds on this path** (spike crashed the dispatched sub-agent mid-run; its completed inner step was not recomputed). Re-confirm once on `PostgresDurableCheckpointer` per S3.
- This routing must be active only for ReAct (Topology 1) wiring; it shares the **same** subagent node the Supervisor uses (do not fork a second subagent node).
- **`graph.py` is a worktree-contended file** (P1 / S3 / S6 / S8 also touch it). If working in parallel with another agent that edits `graph.py`, use `isolation: "worktree"` and merge after (§A3, §A9).

### Preset wiring note (informational — actual preset defaults are S2)

The presets that enable `dispatch_subagent` in their default allowlist are **`coding`** and **`investigation`**. S2 (`PresetDefaults`) owns seeding those allowlists; S4 only needs the *gating* to honor whatever allowlist the agent resolves to. Do not hardcode preset names in `_get_tools` — gate purely on the resolved allowlist containing `dispatch_subagent`.

### Consumer expectations

This task lands the Topology-1 tool ONLY. Do NOT:
- Build or fork fan-out machinery (it lives in S3's `run_subagent`).
- Build the Supervisor graph or `Send` fan-out (S6).
- Branch `_build_graph` on topology (S8).
- Emit `task_events` types `subagent_started/finding/failed` (S9).
- Add `dispatch_subagent` to any preset's default allowlist (S2) or touch the API/Console.

## Acceptance Criteria (observable behaviors)

- [ ] With `dispatch_subagent` in the agent's allowlist, `_get_tools` registers exactly one `dispatch_subagent` tool, closure-bound over runtime context; the LLM can emit it.
- [ ] With `dispatch_subagent` **absent** from the allowlist, the tool is **not** registered (a non-delegating / `chat` agent never sees it).
- [ ] An emitted `dispatch_subagent(prompt, tools, budget)` tool call is **routed via `Send` to the shared subagent node** (the post-agent routing edge — **not** executed inside the ToolNode), reaching `run_subagent` with `ceiling == budget` and `depth` read from graph state (verified with a spy on the subagent node / `run_subagent`).
- [ ] A **success** `SubagentResult` threads back to the parent as a `ToolMessage` **carrying the original `tool_call_id`**, with the structured summary as content; the sub-agent's internal working messages are **not** on the parent's `messages` channel (isolation — they stay on the separate channel).
- [ ] A **failure marker** (`ceiling`/`timeout`/`error`/`depth`) threads back as a descriptive `ToolMessage` keyed to the original `tool_call_id` — the parent loop continues; **no** exception aborts the parent graph.
- [ ] **Mixed turn:** when the LLM emits a `dispatch_subagent` call **alongside** a normal tool call in one turn, the dispatch is `Send`-routed and the normal call goes to the ToolNode, and **every** `tool_call_id` in the turn gets **exactly one** `ToolMessage` before the next agent-node call (no unanswered tool call — provider API would otherwise error).
- [ ] The LLM **cannot** set `depth` — depth is sourced from graph state regardless of tool args; a crafted call cannot escalate it past `MAX_SUBAGENT_DEPTH`.
- [ ] Registering `dispatch_subagent` respects `MAX_TOOLS_PER_AGENT`; an agent already at the cap raises the existing too-many-tools error rather than silently exceeding it.
- [ ] No `parent_task_id` / `sub_agent_id` / `waiting_for_subagent` symbol appears in the new files (grep-asserted in test or review).
- [ ] The new unit-test file binds **no** TCP ports and spawns **no** server subprocess (worktree-concurrency-safe).
- [ ] The narrowest worker test scope passes via the pinned venv.

## Testing Requirements

- **Unit tests (fake model / spied subagent node / `run_subagent`, no network):**
  - allowlist-gating on (tool registered) and off (tool absent);
  - **routing:** an emitted `dispatch_subagent` call is `Send`-routed to the subagent node (not run in the ToolNode), with args pass-through (`budget → ceiling`, `prompt`, `tools`) and `depth` from state, not args;
  - success → `ToolMessage` keyed to the original `tool_call_id` with the summary; sub-agent internal messages **not** on the parent `messages` channel;
  - each failure marker → `ToolMessage` (keyed to `tool_call_id`) describing the reason, parent loop survives;
  - **mixed turn:** `dispatch_subagent` + a normal tool call → dispatch `Send`-routed, normal call to ToolNode, **every** `tool_call_id` answered with exactly one `ToolMessage` before the next agent-node call;
  - `MAX_TOOLS_PER_AGENT` interaction (cap respected).
- **No new fan-out logic to test** — the ceiling/timeout/depth *enforcement* is S3's; S4 tests assert *routing-edge delegation + `tool_call_id`-keyed result-injection + mixed-turn answering + gating*.
- All tests **worktree-concurrency-safe**: no hardcoded ports, no fixed server subprocess.

## Constraints and Guardrails

- **Route through `Send`, don't run in the ToolNode.** `dispatch_subagent` is a driver over S3's shared subagent node: a **post-agent routing edge** intercepts the tool call and `Send`s it to that node — it is **not** executed inside the ToolNode (running it there would bury the multi-turn sub-agent in one parent super-step and forfeit per-inner-turn crash resume). Do not reimplement the ceiling / heartbeat / timeout / depth machinery and do not fork a second subagent node — both live in / are shared with S3 (§A9).
- **Mixed turns: answer every `tool_call_id` exactly once.** When a turn mixes a `dispatch_subagent` call with normal tool calls, split them (dispatch → `Send`, others → ToolNode); every emitted `tool_call_id` must get exactly one `ToolMessage` (the dispatch result keyed to its `tool_call_id`) before the next agent-node call — an unanswered tool call makes the provider API error.
- **`graph.py` graph wiring is worktree-contended.** This task edits `executor/graph.py`'s graph construction (routing/conditional edges), not just `_get_tools`. If running alongside another agent that touches `graph.py` (P1 / S3 / S6 / S8), use `isolation: "worktree"` and merge after (§A3, §A9).
- **Pattern A only.** No sub-agent task rows, `parent_task_id`, per-sub-agent lease, `sub_agent_id` ledger column, or `waiting_for_subagent`. If delegation seems to need a cross-task spawn, STOP — it does not (§A0 invariant 1).
- **Depth from state, not from the LLM.** The tool reads `depth` from graph state; the LLM cannot set or escalate it. Cap is 2; sub-agents are ReAct-only (§A0 invariant 7).
- **Headless sub-agents — `tools` arg subject to the interrupt filter.** The `tools` arg the LLM passes is filtered by `run_subagent` to exclude any `interrupt()`-bearing tool (`request_human_input` and any future pause tool); the LLM cannot smuggle an interrupt tool into a sub-agent. Do not bypass or duplicate this filter — enforcement is S3's; S4 relies on it (§A0 inv. 8, §A11-E3).
- **Failure returns, never raises.** A failure marker becomes a `ToolMessage`, not a graph error — the parent LLM sees and reacts.
- **Allowlist-gated; respect `MAX_TOOLS_PER_AGENT`.** Gate on the resolved allowlist (not hardcoded preset names); count against the existing tool cap.
- Do not add the tool to any preset's allowlist (that is S2), build the Supervisor graph (S6), branch `_build_graph` (S8), add `task_events` types (S9), or touch API/Console.

## Assumptions

- **S3 is complete** and `executor/subagents/fanout.py::run_subagent` exposes the contract in its spec (`SubagentResult` success/failure, `MAX_SUBAGENT_DEPTH`, model/event-callable injection).
- The `build_memory_tools` registration block (`graph.py:1233-1238`) is the canonical pattern for an allowlist-gated, closure-bound built-in tool; the cap check at `:1262` applies uniformly.
- The agent's resolved tool allowlist is available in `_get_tools`'s scope (it already resolves built-in vs. BYOT tools there).
- LangGraph is pinned at **1.0.5**; `Send` (map-reduce dispatch), conditional/routing edges after a node, and a `ToolMessage` keyed to a `tool_call_id` appended to the parent's `messages` channel (satisfying the provider's "every tool call answered" requirement) all behave as in the parent runtime. Verify against the installed version, not memory — the routing edge that turns a `dispatch_subagent` tool call into a `Send` (rather than executing it in the ToolNode) is the load-bearing mechanism here.

<!-- AGENT_TASK_END: task-s4-dispatch-subagent-tool.md -->
