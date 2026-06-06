<!-- AGENT_TASK_START: task-s3-shared-fanout-helper.md -->

# Task S3 — Shared In-Process Fan-Out Helper: `run_subagent`

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections **"Shared fan-out machinery"**, **"Execution model: in-process fan-out (Pattern A)"** (esp. the **Wiring requirement** paragraph — the sub-agent is a **compiled subgraph node reached by `Send`, sharing the parent checkpointer**, *not* an imperatively-`ainvoke`d subgraph), **"Durability"**, and the `dispatch_subagent` subsection under *Delegation tools available in ReAct*. These define the contract you are building. Note the per-sub-agent **ceiling**, **heartbeat**, **timeout**, and **depth-2** guardrails are all enforced *here*, in the helper / graph state — not in the task/lease layer.
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — **§A0 invariant 1** (Pattern A only — no sub-agent task rows, no `parent_task_id`, no per-sub-agent lease, no `sub_agent_id` ledger column, no `waiting_for_subagent`), **§A0 invariant 7** (depth cap 2; sub-agents are ReAct-only, never Supervisor), **§A4.1 row S3** (the canonical output contract — names are load-bearing), **§A5** (the `run_subagent` integration row: "ceiling/timeout exhaustion → structured failure marker, not graph error"), **§A7** (`subagent.heartbeat` event semantics), and **§A8** ("Sub-agent heartbeat vs. parent heartbeat confusion" risk row).
3. `services/worker-service/executor/graph.py` — the existing ReAct construction as the pattern to mirror at sub-agent scope: `_build_graph` (`:1059`), `_get_tools` (`:850-1057`), `llm.bind_tools` (`:1269`), `StateGraph(state_type)` (`:1555`), and the `astream(... durability="sync")` invocation (`:3127`). A sub-agent subgraph is a *smaller, isolated-context* version of this same ReAct loop.
4. `services/worker-service/executor/compaction/state.py` — `RuntimeState` (`:102`) and its reducer conventions (`Annotated[..., reducer]`). The per-sub-agent ceiling/turn counters and `depth` live in the subgraph's state, modelled on these.
5. `services/worker-service/core/heartbeat.py` — `HeartbeatManager` / `build_heartbeat_query` (`:24`), which updates `tasks.lease_expiry`. **Read this to understand what your helper must NOT touch:** the parent's lease heartbeat stays here and only here; your `subagent.heartbeat` is an *event*, not a lease extension.
6. `services/worker-service/core/cost_ledger_repository.py` — `insert_cost_row` (`:65`), `sum_task_cost` (`:112`). Read only to confirm you are **not** adding a per-sub-agent ledger row; sub-agent LLM cost rolls into the parent's super-step `agent` operation (Pattern A).

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests that cover this change with the pinned venv — e.g. `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_subagent_fanout.py -q` (your new file), or via the isolated harness `make e2e-test PYTEST_ARGS='-k subagent'`. **Never** run raw `pytest tests/backend-integration` in a worktree (it hits fixed default ports). Fix any regression before moving on.
2. Update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S3 status and note that the helper is the shared primitive S4 and S6 depend on.

## Context

The Agent Modes design fans out sub-agents **in-process (Pattern A)**: a sub-agent is a **compiled LangGraph subgraph declared as a node** of the parent graph, **reached by `Send`** and **sharing the parent's checkpointer** (a namespaced sub-checkpoint per branch), that runs inside the parent's run — one `thread_id`, one task row, one checkpoint stream — never a separate durable child task. This wiring is **load-bearing, not an implementation detail**: a `Send`-reached subgraph node checkpoints its **inner** super-steps under the namespace, so a crash mid-sub-agent resumes **per inner turn** (completed turns restored, only the interrupted turn re-runs) and the sub-agent's full transcript is persisted in its sub-checkpoint. The earlier "drive a compiled subgraph via `astream`/`ainvoke` imperatively" option is **rejected** — an imperatively-invoked subgraph runs inside one parent super-step and forfeits per-inner-turn durability (a crash re-runs the whole sub-agent). Two drivers fan out over the **same** machinery and **both route through `Send` to this same shared subagent node**: the LLM emitting a `dispatch_subagent` tool call (Topology 1, Task S4 — a post-agent routing edge intercepts the call and `Send`s it) and the Supervisor graph structurally `Send`-ing N sub-agents (Topology 2, Task S6). This task builds that machinery **once** as `run_subagent`, so neither driver forks it.

Because a whole fan-out is one super-step and Track 3 meters cost at the checkpoint boundary, the per-task budget cap alone does **not** bound a single step — N multi-turn sub-agents can accrue spend before the meter next reads. The helper closes this with a **per-sub-agent token+turn ceiling** in graph state. The parent's lease also stays healthy while it `await`s the fan-out, so a wedged sub-agent branch is only inferable from silence; the helper emits a **per-sub-agent heartbeat event** and enforces a **per-sub-agent timeout** to make liveness observable and bound runaway branches. A **depth counter** (cap 2) prevents a buggy prompt from recursing into a fork-bomb before budget trips.

This task is worker-only and infrastructure-only. It does not register a tool, build the Supervisor graph, branch `_build_graph`, or touch the API/Console. Those are S4 / S5–S7 / S8.

## Task-Specific Shared Contract

`executor/subagents/fanout.py::run_subagent(prompt, tools, *, ceiling, depth, ...) -> SubagentResult` (the exact keyword-only extras — model handle, task/tenant identifiers, an event-emit callable, a timeout value — are the implementer's to shape from the existing ReAct construction; the load-bearing names are `run_subagent`, `ceiling`, `depth`, `SubagentResult`, `MAX_SUBAGENT_DEPTH`, and the failure-marker shape).

- **Sub-agent is a compiled subgraph node reached by `Send`, sharing the parent checkpointer.** The helper builds the sub-agent as a **compiled ReAct subgraph declared as a node** that drivers reach **via `Send`** and that **shares the parent's checkpointer** (a namespaced sub-checkpoint per branch). This is the load-bearing wiring: it is what gives **per-inner-turn crash resume** and a **persisted sub-agent transcript** (see *Execution model* → *Wiring requirement*). **Do NOT** build the sub-agent as a subgraph the helper `astream`/`ainvoke`s imperatively inside a node/tool function — that option is **explicitly rejected** (it runs inside one parent super-step and forfeits per-inner-turn durability).
- **Isolated context via a SEPARATE internal message channel.** Each call runs a self-contained ReAct loop with **its own context window** (a fresh message list seeded from `prompt`, not the parent's history) and **its own tool allowlist** (`tools`, the caller-supplied subset). The sub-agent's working messages live on their **own channel** (e.g. `sub_messages`) — **NOT** the parent's `messages` channel — so they do not leak into the parent's context, while still being **checkpointed (persisted)** under the sub-checkpoint namespace (which is what E5 / the Console drill-in reads). The sub-agent is *always* ReAct — never Supervisor topology.
- **Sub-agents are headless — filter out `interrupt()`-bearing tools.** Before binding `tools` to the sub-agent, the helper **MUST** filter the passed allowlist to **exclude any `interrupt()`-bearing tool** (`request_human_input` — `tools/definitions.py:449` — and any future pause tool). Rationale (verified in LangGraph 1.0.5): if ≥2 sub-agents call `interrupt()` in one `Send` super-step, the worker's scalar `Command(resume=...)` path (`graph.py:3108-3112`; detection reads only `interrupts[0]` at `:3283-3308`) raises `RuntimeError: When there are multiple pending interrupts, you must specify the interrupt id` — a latent unresumable-task bug. So a sub-agent allowlist containing `request_human_input` must not get it bound. HITL clarification is **Scope's job only** (a single interrupt at a clean node boundary). (§A0 inv. 8, §A11-E3.)
- **Ceiling enforced in graph state.** A per-sub-agent **token + turn** ceiling lives in the subgraph's state (model on `RuntimeState`'s `Annotated[..., reducer]` counters). When either the token budget or the turn count is exhausted, the loop stops and the call returns a **failure marker** with `reason: "ceiling"` — it does **not** raise a graph error.
- **Heartbeat is a Langfuse-span EVENT, not a lease touch and not a `task_events` row.** While the sub-agent runs, the helper emits a `subagent.heartbeat` event (via the caller-supplied emit callable). The **DECIDED sink is a Langfuse span event** (§A11-E4): it is **NOT** a `task_events` row (S9 does **not** add a `subagent_heartbeat` CHECK value to migration 0025) and it does **NOT** touch `tasks.lease_expiry` / call into `core/heartbeat.py` — the parent task's single lease heartbeat stays the parent's alone (per §A0 / §A8). The heartbeat is load-bearing: it is the design's **only wedged-branch detector** (the parent lease stays healthy while it `await`s the fan-out, so silence is the only signal). It is purely for observability ("this branch is alive"); silence ≠ liveness.
- **Timeout enforced per sub-agent.** A wall-clock timeout bounds a wedged branch (the parent's lease stays healthy while awaiting, so the helper must self-police). On timeout the call returns a failure marker with `reason: "timeout"` — again, not a raised graph error.
- **Depth counter, cap 2.** `depth` is read from the caller and incremented for the sub-agent's own context. `MAX_SUBAGENT_DEPTH = 2` (a module constant). A call with `depth > MAX_SUBAGENT_DEPTH` is rejected — return a failure marker (`reason: "depth"`) rather than spawning. (A Supervisor's structural fan-out consumes one level just as a `dispatch_subagent` call does, so a Supervisor → ReAct-sub → `dispatch_subagent` chain reaches the cap.)
- **Return shape — `SubagentResult`.** On success: a **structured summary** (the sub-agent's distilled output — the text/result the driver will inject). On ceiling / timeout / error: a **structured FAILURE MARKER** carrying a `reason` in `{ceiling, timeout, error, depth}`. Both success and failure are *returned values*, never exceptions that abort the parent graph — the driver (S4 injects a `ToolMessage`; S6 merges into the `subagent_results` reducer) decides what to do with a failure. This is what makes "one flaky sub-agent never sinks the run" possible upstream.
- **Cost rolls into the parent (Pattern A).** Any LLM spend inside the sub-agent is attributed to the **parent's** super-step `agent` operation at the parent's `checkpoint_id`. **Do NOT** insert a per-sub-agent cost-ledger row and **do NOT** add a `sub_agent_id` column anywhere.
- **Per-inner-turn crash resume + persisted transcript.** Because the sub-agent is a `Send`-reached subgraph node sharing the parent checkpointer, a worker crash mid-sub-agent resumes the parent's run **at the inner turn the sub-agent died on** — its earlier inner turns are restored (not re-run, tokens not re-spent), completed sibling sub-agents are untouched (spike #2/#3, 2026-06-05). The sub-agent's full transcript is **persisted in its namespaced sub-checkpoint** — this is the read path E5 / the Console drill-in uses; **no new transcript table or store** is added here.
- **`durability="sync"`.** The sub-agent is a **`Send`-reached subgraph node**, not an imperatively-`ainvoke`d subgraph. It inherits the parent runtime's `durability="sync"` (`executor/graph.py:3127`) — the per-super-step synchronous persistence that makes per-inner-turn resume work — so the parent's fan-out must drive with `durability="sync"`; do **not** fall back to LangGraph's `"async"` default.
- **Unit-testable with a fake model (no network).** The helper accepts an injected model/LLM handle so a fake (canned tool-call → canned response) drives the ReAct loop deterministically. Ceiling, timeout, and depth paths are exercisable without any real provider call.

## Affected Component

- **Service/Module:** Worker Service — Agent Modes / sub-agent fan-out
- **File paths:**
  - `services/worker-service/executor/subagents/fanout.py` (new — `run_subagent`, `SubagentResult`, `MAX_SUBAGENT_DEPTH`, failure-marker construction)
  - `services/worker-service/executor/subagents/__init__.py` (new — package init, export the public surface)
  - `services/worker-service/tests/test_subagent_fanout.py` (new — fake-model unit tests; **worktree-concurrency-safe**, no bound ports)
  - Possibly a small extension to `executor/compaction/state.py` **only** if the subgraph reuses `RuntimeState` and needs additive ceiling/turn/`depth` counter channels. If so, additive `Annotated[..., reducer]` fields only — do not change existing channels' reducers. (Coordinate: this file is shared with the Planning Primitive track / S6; if working in parallel, use `isolation: "worktree"` and merge.)
- **Change type:** new package + helper; optional additive state-channel extension

## Dependencies

- **Must complete first:** None for the helper's own construction. (If the subgraph reuses `RuntimeState`, the additive state channels should land before S4/S6 build on them — sequence the combined `state.py` extension first, or worktree-isolate per §A3.)
- **Provides output to:** **S4** (`dispatch_subagent` tool wraps `run_subagent`) and **S6** (Supervisor structural `Send` fan-out drives `run_subagent`). **S3 is the hard blocker for both** — land it green first.
- **Shared interfaces/contracts:** the `run_subagent` signature, `SubagentResult` success/failure shapes, `MAX_SUBAGENT_DEPTH`, and the `subagent.heartbeat` event name.

## Implementation Specification

### New module: `executor/subagents/fanout.py`

1. **`MAX_SUBAGENT_DEPTH = 2`** module constant.
2. **`SubagentResult`** — a structured type (dataclass / TypedDict) with at minimum: an outcome discriminator (success vs. failure), the structured summary on success, and a `reason ∈ {ceiling, timeout, error, depth}` on failure. The driver inspects this; it never has to catch an exception to detect sub-agent failure.
3. **`run_subagent(prompt, tools, *, ceiling, depth, ...)`** — async. Steps:
   - Reject `depth > MAX_SUBAGENT_DEPTH` → return a `depth` failure marker (no spawn).
   - **Filter `tools` to exclude any `interrupt()`-bearing tool** (`request_human_input` and any future pause tool) before binding — sub-agents are headless (§A0 inv. 8, §A11-E3). The filtered-out tool is silently dropped from the sub-agent's allowlist; it is never bound.
   - Build the sub-agent as a **compiled ReAct subgraph node reached by `Send`, sharing the parent checkpointer** (namespaced sub-checkpoint) — **not** a subgraph the helper `astream`/`ainvoke`s imperatively. Drive the fan-out with `durability="sync"` (inherited from the parent runtime).
   - Build an **isolated-context** ReAct loop seeded from `prompt` with the (filtered) `tools` allowlist, mirroring the parent ReAct construction (`_get_tools` / `llm.bind_tools` shape at sub-agent scale). The sub-agent's working messages live on a **separate internal channel** (e.g. `sub_messages`), **not** the parent's `messages` channel — its message list is its own, do not pass the parent's history in, and do not leak its working messages into the parent context. (They are still checkpointed under the sub-checkpoint namespace for durability + drill-in.)
   - Track per-sub-agent **token spend and turn count** in graph state; stop and return a `ceiling` failure marker when either is exhausted.
   - Emit a `subagent.heartbeat` event periodically while running, via the injected emit callable — to the **Langfuse-span sink** (a span event, NOT a `task_events` row). **Never** touch `tasks.lease_expiry` / `core/heartbeat.py`.
   - Enforce a wall-clock **timeout**; on expiry return a `timeout` failure marker.
   - On normal completion, return a success `SubagentResult` carrying the structured summary.
   - On an unexpected internal error, catch it and return an `error` failure marker (do not let it propagate as a graph error).
   - Attribute any LLM cost to the parent super-step (no per-sub-agent ledger row).

### New module: `executor/subagents/__init__.py`

Export `run_subagent`, `SubagentResult`, `MAX_SUBAGENT_DEPTH`.

### Consumer expectations

This task lands the primitive ONLY. Do NOT:
- Register the `dispatch_subagent` tool or any tool (that is S4).
- Build the Supervisor graph, `Send` fan-out, or the `subagent_results` reducer (that is S6).
- Branch `_build_graph` on `topology` (that is S8).
- Emit `task_events` rows of types `subagent_started/finding/failed` (that is S9; the `subagent.heartbeat` here is the helper's own liveness event, distinct from S9's CHECK-constrained `task_events` types).
- Touch the API or Console.

## Acceptance Criteria (observable behaviors)

- [ ] `run_subagent(prompt, tools, ceiling=..., depth=0, ...)` driven by a **fake model** that returns a final answer runs to completion and returns a **success** `SubagentResult` carrying the structured summary — no network call.
- [ ] A fake model that loops past the **turn** portion of `ceiling` causes the call to stop and return a **failure marker** with `reason == "ceiling"` (not a raised exception).
- [ ] A fake model whose token spend crosses the **token** portion of `ceiling` likewise returns a `ceiling` failure marker.
- [ ] A sub-agent that exceeds its **timeout** returns a `timeout` failure marker (not a raised exception). Test uses a short injected timeout — no real wall-clock dependence beyond a small bound.
- [ ] Calling `run_subagent(..., depth=MAX_SUBAGENT_DEPTH + 1, ...)` returns a `depth` failure marker and does **not** spawn a subgraph.
- [ ] A sub-agent given `request_human_input` (or any `interrupt()`-bearing tool) in its `tools` allowlist does **not** get it bound — a unit test asserts the filter drops it before `bind_tools`, so the sub-agent's bound tool set excludes it (§A0 inv. 8, §A11-E3).
- [ ] A **long-running** sub-agent emits **≥1 `subagent.heartbeat` span event** (Langfuse span sink) via the injected emit callable; a spy/fake confirms the event lands as a span event and **not** as a `task_events` row, that **`tasks.lease_expiry` is never written**, and that `core/heartbeat.py` is never called from the helper (§A11-E4).
- [ ] No code path in `fanout.py` calls `insert_cost_row` with a per-sub-agent key, and no `sub_agent_id` / `parent_task_id` / `waiting_for_subagent` symbol appears anywhere in the new files (grep-asserted in test or review).
- [ ] The sub-agent is wired as a **`Send`-reached compiled subgraph node sharing the parent checkpointer** (namespaced sub-checkpoint), **not** an imperatively-`ainvoke`d subgraph; the fan-out is driven with `durability="sync"` (a test / review asserts the wiring and that no imperative `ainvoke`-of-subgraph path exists in `fanout.py`).
- [ ] The sub-agent's working messages are on a **separate channel** (e.g. `sub_messages`), not the parent's `messages` channel — a test asserts the parent's `messages` does not contain the sub-agent's internal turns, while the sub-checkpoint namespace does.
- [ ] **Per-inner-turn crash resume:** a sub-agent crashed at inner turn _k_ resumes at turn _k_ (earlier turns restored, not re-run; tokens not re-spent), and completed sibling sub-agents are not recomputed (spike #2/#3).
- [ ] **Transcript persistence:** after a sub-agent runs, its full turn-by-turn transcript is readable from its **namespaced sub-checkpoint** (the E5 / Console drill-in read path) — with **no new table or transcript store** added.
- [ ] **Confirmed once against `PostgresDurableCheckpointer`:** the per-inner-turn resume + transcript persistence behaviors above are verified at least once against the real `PostgresDurableCheckpointer` (the spikes used in-process `MemorySaver`; the same `pending_writes` / namespace API is *expected* to hold — **verify, do not assume**). The remaining fake-model unit tests may stay on `MemorySaver`.
- [ ] The new unit-test file binds **no** TCP ports and spawns **no** server subprocess (worktree-concurrency-safe).
- [ ] The narrowest worker test scope passes via the pinned venv.

## Testing Requirements

- **Unit tests (fake model, no network):** success path; ceiling-by-turns; ceiling-by-tokens; timeout; depth-rejection; **interrupt-tool-filtered** (`request_human_input` in the allowlist is not bound to the sub-agent); **heartbeat-emitted-as-span-event-without-lease-touch** (≥1 `subagent.heartbeat` span event, no `task_events` row, no `tasks.lease_expiry` write, no `core/heartbeat.py` call).
- **Isolation assertions:** the sub-agent's context window is its own (parent history not threaded in); the failure marker is a *return value*, not an exception.
- **Mostly no DB / no E2E for the helper itself** — it is pure-ish and model-injected; the ceiling/timeout/depth/heartbeat/filter paths run on an in-process `MemorySaver`. (The full `subagent_results` reducer is exercised by S6/S11, not here.) **One exception:** confirm the per-inner-turn crash resume + namespaced-transcript persistence **once against `PostgresDurableCheckpointer`** (the spikes used `MemorySaver`; verify the same `pending_writes`/namespace behavior on real Postgres, don't assume). That one check uses the isolated test harness; the rest stay DB-free.
- All tests must be **worktree-concurrency-safe**: no hardcoded ports, no fixed server subprocess. (If a future test ever needs a port, use `:0` / `scripts/e2e/free-port.py` per the pattern in `tests/test_mcp_http_integration.py`.)

## Constraints and Guardrails

- **Pattern A only — do NOT build Pattern B.** No sub-agent task rows, no `parent_task_id` column/tree, no per-sub-agent lease, no `sub_agent_id` cost-ledger column, no `waiting_for_subagent` pause state. If the implementation seems to need cross-task spawn-and-await, STOP — it does not; that is the deferred upgrade path, explicitly out of scope (§A0 invariant 1, §A9).
- **Sub-agents are headless — exclude `interrupt()`-bearing tools.** The helper must filter `request_human_input` (and any future `interrupt()`-bearing pause tool, `tools/definitions.py:449`) out of the passed `tools` before binding them to the sub-agent. A fan-out where a sub-agent could `interrupt()` is a latent unresumable-task bug (≥2 pending interrupts → the worker's scalar `Command(resume=...)` raises `RuntimeError` in LangGraph 1.0.5; `graph.py:3108-3112`, `:3283-3308`). HITL clarification is Scope's job only (§A0 inv. 8, §A11-E3).
- **Heartbeat is a Langfuse-span event, not a lease and not a `task_events` row.** The helper must never extend `tasks.lease_expiry` or invoke `core/heartbeat.py`; the parent's lease heartbeat is the parent's alone (§A8). The decided sink is a **Langfuse span event** — the implementer **MUST NOT** add a `subagent_heartbeat` value to migration 0025's `task_events` CHECK constraint (that migration is S9's; `subagent.heartbeat` is not a `task_events` type). (§A11-E4.)
- **Sub-agents are ReAct-only.** Never construct a Supervisor topology inside `run_subagent`. Depth cap is 2 (§A0 invariant 7).
- **Failure is a return value.** Ceiling / timeout / error / depth never raise out of `run_subagent` to abort the parent graph — they return a structured failure marker so the driver can decide (re-dispatch, proceed, or fail-only-if-zero-returned upstream).
- **Cost is the parent's.** Do not add per-sub-agent ledger rows or any budget-refund path; cost is cumulative (§A9). No new budget exemptions beyond what Track 3 already carves.
- **Sub-agent is a `Send`-reached subgraph node sharing the parent checkpointer — NOT an imperative `ainvoke`.** This wiring is the durability story (per-inner-turn resume + persisted transcript). The "drive a compiled subgraph via `astream`/`ainvoke` imperatively inside a node/tool" option is **rejected** — do not build it; it forfeits per-inner-turn durability.
- **Isolated context lives on a separate internal channel.** The sub-agent's working messages are on their own channel (e.g. `sub_messages`), never the parent's `messages` channel — checkpointed under the sub-checkpoint namespace, but not leaked into the parent context.
- **`durability="sync"`** — do not switch to LangGraph's `"async"` default.
- **Confirm the durability behavior once against `PostgresDurableCheckpointer`.** The per-inner-turn resume + namespaced-transcript persistence must be verified at least once against the real Postgres checkpointer, not only the spikes' in-process `MemorySaver` (same `pending_writes`/namespace API *expected* — verify, don't assume).
- **Build the machinery once.** This is the single shared primitive; S4 and S6 are *drivers* over it. Do not let either driver fork a second copy.
- Do not register any tool, build the Supervisor graph, branch `_build_graph`, add `task_events` types, or touch API/Console here.

## Assumptions

- LangGraph is pinned at **1.0.5**; `Send`, subgraphs, and `durability="sync"` behave as in the parent runtime (`executor/graph.py:3127`). Verify against the installed version, not memory.
- The parent ReAct construction in `executor/graph.py` (`_build_graph`, `_get_tools`, `llm.bind_tools`) is the canonical pattern for an isolated-context ReAct loop at sub-agent scale.
- A model/LLM handle and an event-emit callable can be **injected** so the helper is testable with a fake model offline.
- Reusing `RuntimeState` (with additive ceiling/turn/`depth` channels) is acceptable; if instead a slimmer sub-agent state type is cleaner, that is an implementer call as long as the contract above holds.

<!-- AGENT_TASK_END: task-s3-shared-fanout-helper.md -->
