# Agent Modes — Design

**Status:** Approved framing (brainstorm 2026-05-21). Implementation tracks that flow from this design are described in the *Implementation Tracks* section; each becomes its own track + plan when prioritized.

**Scope:** A cross-cutting architectural framing that governs how the platform exposes *agent shapes* to customers. Informs Track 8 and the implementation tracks that flow from this framing (Planning Primitive, Supervisor topology, Workflow resource, Presets, `dispatch_subagent` tool — the former Tracks 9 and 10 are subsumed here). Not itself an implementation track.

**Supersedes:** the previous `phase-2/track-9-planning-primitive.md` stub and the Track 10 ("Deep Research Mode") section of `phase-2/design.md`. Those concepts are now sub-elements of the framing established here.

---

## Why this doc exists

Phase 2 has accumulated several track-level proposals that all touch the same underlying question: *what shape does a customer's agent take?* Planning Primitive (was Track 9), Deep Research Mode (was Track 10), and Coding-Agent Primitives (Track 8) each defined a slice of agent behavior in isolation, and the cross-cutting question — *how do these compose, and what's the right cut between "agent topology" and "agent capability"?* — was never answered.

Without that framing, three failure modes emerge:

1. **Track-by-track drift.** Each track defines its own configuration surface (`planning.enabled`, `mode = 'deep_research'`, etc.) without a unifying mental model. Customers see a bag of feature flags.
2. **Topology vs. capability confusion.** Track 10 calls deep research a "mode" implying a separate graph; Track 9 says the same ReAct loop can do planning. Either deep research is a different runtime or it's a configured ReAct loop — both can't be true.
3. **Overlapping or duplicate work.** Track 9's "plan" and Track 10's "research tree" sound similar enough that they might end up sharing a schema, when they're actually structurally different.

This doc resolves the framing so the track-shaped work that follows has a coherent home.

## The framing

**Two topologies, two delegation tools, one resource type, and presets layered on top.**

```
┌──────────────────────────────────────────────────────────────┐
│  Topology (graph shape — fixed at agent creation)            │
│  ├── ReAct (default)                                         │
│  └── Supervisor (customer-facing: "Deep Research")           │
│      Scope → Supervisor (with iteration) → Subagents → Writer│
├──────────────────────────────────────────────────────────────┤
│  Delegation tools (available inside ReAct, optional)         │
│  ├── dispatch_subagent  (ad-hoc in-process child agent)      │
│  └── execute_workflow   (invoke a defined Workflow resource) │
├──────────────────────────────────────────────────────────────┤
│  Resource type                                               │
│  └── Workflow definition (customer-prescribed step list)     │
├──────────────────────────────────────────────────────────────┤
│  Presets (named bundles of defaults)                         │
│  ├── chat                                                    │
│  ├── coding                                                  │
│  ├── investigation                                           │
│  ├── research                                                │
│  └── workflow_runner                                         │
└──────────────────────────────────────────────────────────────┘
```

### Why this cut

The honest dimension is **who controls high-level flow**:

- **ReAct**: the LLM controls flow. It decides every turn what to do next, including whether to plan, fan out, critique, or invoke a workflow.
- **Supervisor** (customer-facing: "Deep Research"): the platform controls flow at the high level (Scope runs first; Supervisor delegates to parallel Subagents and may iterate; Writer runs last). The LLM only fills in the steps.
- **Workflow**: the customer (or a workflow definition) controls flow. The LLM executes prescribed steps and cannot reorder.

A given task can sit anywhere on this spectrum:

| Scenario | Topology | Notes |
|---|---|---|
| Chatbot fielding user questions | ReAct | No delegation; pure tool-using loop |
| Coding agent iterating on a repo | ReAct | Planning Primitive on; Track 8 coding tools |
| Coding agent spinning off a focused subtask | ReAct + `dispatch_subagent` | Parent agent decides to delegate |
| Deep research over the web | Supervisor | Fixed graph; fan-out is structural |
| Daily ETL: read S3, transform, write DB | ReAct + `execute_workflow` | Agent invokes a customer-defined workflow |
| Customer onboarding ticket processed end-to-end | `execute_workflow` directly (no ReAct envelope) | Workflow can run standalone, no parent agent needed |

## Topology 1: ReAct (default)

What the runtime already does today: a single LLM in a tool-using loop, durable across crashes via LangGraph checkpointing, paused via Track 2's `waiting_for_input` / `waiting_for_approval` states, compacted by Track 7's tiered transforms.

The LLM decides every turn whether to call a tool, reply, or stop. Customers configure:

- Tool allowlist (built-in + BYOT)
- System prompt
- Planning policy (off / on with platform preamble — the "Planning Primitive" feature; see *How Planning Primitive composes*)
- Compaction policy
- Memory attach (Track 5)
- Budget limits

**Hosts** the majority of customer agents: chat assistants, coding agents, debuggers, investigations, support copilots.

### Delegation tools available in ReAct

These tools turn ReAct into something more powerful without changing the topology:

#### `dispatch_subagent(prompt, tools, budget)`

Runs a focused ReAct sub-agent **in-process** as a LangGraph subgraph with its own context window and tool allowlist, and returns a structured summary to the parent. The `budget` arg is the **per-sub-agent ceiling** (token + turn cap) enforced in graph state by the shared fan-out helper — see *Shared fan-out machinery* for why this is load-bearing, not cosmetic. The sub-agent is *not* a separate task — it executes within the parent's run (same `thread_id`, same checkpoint, same task row); see *Execution model*. Its work is durable as part of the parent's graph state (LangGraph checkpoints the super-step), and its activity surfaces as sub-steps in the parent's event timeline, not as a separate Console row.

The sub-agent is always a **ReAct** agent — sub-agents are never themselves Supervisor topology. Nesting is bounded by a `max_depth` cap (default **2**), carried in graph state and incremented on each `dispatch_subagent` / fan-out level: budget caps bound *cost*, but depth needs its own structural limit, or a buggy prompt can recurse into an expensive fork-bomb before the budget trips. A Supervisor's structural fan-out consumes one level just as a `dispatch_subagent` call does, so a Supervisor → ReAct-sub-agent → `dispatch_subagent` chain reaches the depth-2 ceiling.

Why expose this as a *tool* and not a topology: ReAct agents sometimes benefit from delegating a focused subtask without polluting their own context (e.g., "investigate why test X is flaky"). Making this a tool keeps the parent's topology unchanged; the parent decides when to delegate. *Not a substitute for the Supervisor topology — see below.*

#### `execute_workflow(workflow_def_or_id, inputs)`

Invokes a Workflow resource (see *Workflow as a resource*) as a child task. Returns the workflow's result. The agent uses this when part of its work is well-defined enough to express as a fixed plan rather than freeform LLM iteration.

## Topology 2: Supervisor (customer-facing: "Deep Research")

A four-phase fixed graph with one iteration loop:

```
   ┌───────────────────────────────┐
   │  Scope                        │
   │  ├─ assess query clarity      │
   │  ├─ (if ambiguous) ask user → │  ← waiting_for_input pause
   │  │   wait for response        │
   │  └─ produce brief             │  ← "north star" goal anchor
   └────────────┬──────────────────┘
                │
                ▼
   ┌────────────────────────┐
   │  Supervisor            │  ← iterates: after each
   │  decide subtasks       │     subagent return, may decide
   │  delegate to subagents │     "need more research?"
   └────┬───────────────────┘
        │
   ┌────┼────────────────────┐
   ▼    ▼                    ▼
┌──────┐ ┌──────┐    ┌──────┐
│Sub-  │ │Sub-  │    │Sub-  │   parallel, isolated context,
│agent1│ │agent2│    │agent3│   each emits structured findings
└──┬───┘ └──┬───┘    └──┬───┘
   │        │            │
   └────┬───┴────────────┘
        │  (Supervisor may loop back: "need more")
        ▼
   ┌────────┐
   │ Writer │  one-shot final report with citations
   └────────┘
```

### Two-layer naming and the config model

- **Customer-facing** (API, Console, docs): "Deep Research" — the display label of the `research` preset. Customers never set a topology directly; selecting the `research` preset is how they get this shape. Matches industry terminology (Anthropic, OpenAI, Perplexity).
- **Internal** (codebase, design docs, observability): "Supervisor topology", stored as `agent_config.topology = "supervisor"` — the source of truth for graph shape, set by the preset rather than by the customer. The orchestrator-worker pattern is the academic name; "Supervisor" matches LangChain's terminology for the same shape.

**There is no separate `mode` field.** An earlier draft proposed `agent_config.mode = "deep_research"`; it was dropped because it duplicated the `research` preset and re-created the exact "bag of feature flags" failure mode this doc opens by warning against (see *Why this doc exists*). The field model is: **preset** is the customer-facing selector → it sets the internal **topology** field → which fixes the graph shape at agent creation. **Topology is immutable after creation** — switching shape (e.g. `chat` → `research`) means creating a new agent, not PATCHing an existing one, since the graph is built once at creation; other agent settings remain mutable per Track 1/Track 3. A task targets an `agent_id` (Workflow's `workflow_id` target is a Phase-3 concern; the task schema is not widened for it now — see *Workflow as a resource*).

### Pattern provenance

The four-phase architecture combines proven choices from two production deep-research systems:

| Phase | Choice | Source |
|---|---|---|
| **Scope** (conditional clarify + brief) | LangChain Open Deep Research uses an explicit scoping phase. Justification (LangChain, relaying OpenAI): *"users rarely provide sufficient context in a research request"*, and *"Research is an open-ended task; the best strategy to answer a user request can't be easily known in advance"* ([LangChain blog](https://www.langchain.com/blog/open-deep-research)). The brief *"serves as our north star for success, and we refer back to it throughout the research and writing phases"*. Our refinement: Scope evaluates clarity internally and only asks the user when needed |
| **Supervisor with iteration** | Both systems do this. Anthropic: *"The LeadResearcher synthesizes these results and decides whether more research is needed"* ([Anthropic blog](https://www.anthropic.com/engineering/multi-agent-research-system)). LangChain: the supervisor spawns subagents dynamically (a supervisor-decided count) with reflection |
| **Parallel Subagents with isolated context** | Both systems. Anthropic describes an agent that plans, then "uses tools to create parallel agents that search for information simultaneously." The structural fan-out is the load-bearing piece behind Anthropic's reported 90.2% improvement — of an **Opus-4-lead + Sonnet-4-subagent** system over single-agent Claude Opus 4 on their internal research eval (the win bundles fan-out with a cheap-subagent model split) |
| **One-shot Writer** | LangChain's hard-learned lesson: *"[we] restrict[ed] multi-agent to research, and write the report in one-shot"* after parallel section-writing caused coordination problems |

### What the Supervisor topology owns

- **Scope prompt template** — clarity assessment + clarification questions + brief generation
- **Supervisor prompt template** with iteration decision protocol and a structured subtask-emission contract (the Supervisor's output is parsed into `subtasks: [...]`, not freeform text)
- **Subagent prompt template** emitting structured findings (`finding_id` + claim + source + supporting quote), not freeform prose (see *Citation binding*)
- **Writer prompt template** that cites by `finding_id` (see *Citation binding*)
- **Citation verification pass** — a thin, single-purpose node that confirms each cited quote supports its sentence
- **Per-iteration parallel fan-out**: the graph reads the Supervisor's subtask list and `Send`s N sub-agents in parallel, in-process (see *Execution model* and *Shared fan-out machinery* below)
- **Fan-out and iteration caps**: hard limits on sub-agents-per-iteration and total Supervisor → Sub-agents loops to prevent runaway (also the v1 bound on worker-slot occupancy — see *Execution model*)

### Subagent count — dynamic, capped

Subagent count is **not fixed**. The Supervisor decides per iteration how many subtasks to emit, based on how it decomposes the brief. Both primary sources confirm this is the right model: Anthropic's blog notes their early Lead Researchers *"made errors like spawning 50 subagents for simple queries"* before they tuned the decomposition, and LangChain's Supervisor delegates to "an appropriate number of sub-agents" decided dynamically. Our customer-configurable caps (`max_fanout_per_iteration`, `max_iterations`) bound the cost envelope; the LLM picks the actual count within those bounds.

### Partial subagent failure

A subagent that fails (error, budget exhaustion, cancellation) does **not** error the whole graph. The Supervisor node receives the partial result set — successful subagents' findings plus a failure marker for each that didn't return — and decides how to proceed: re-dispatch the failed subtask next iteration, proceed with what it has, or, **only if zero subagents returned**, fail the task. Fail-fast applies to the all-failed case alone; one flaky web fetch must not sink an otherwise-complete research run.

Re-dispatching across rounds needs a stable logical **`subtask`** identifier distinct from the `iteration` (round) marker: the round-2 retry of a failed subtask must link back to its round-1 attempt, or the Console tree shows them as two unrelated entries instead of "subtask X: failed (round 1) → succeeded (round 2)." Since sub-agents run in-process (see *Execution model*), these are **markers in graph state / on emitted events**, not columns on separate task rows. See *Observability*.

### Citation binding

Citations are bound deterministically rather than left to the Writer's discretion:

1. **Subagents emit structured findings**, each `{finding_id, claim, source_url, supporting_quote}` — not freeform prose with inline links.
2. **The Writer cites by `finding_id` only.** It cannot invent a source: an ID either resolves to a real finding or it doesn't. The runtime resolves IDs → full citations at render time, with no LLM involvement.
3. **A thin verification pass** takes each cited sentence plus its referenced finding's quote and confirms the quote actually supports the sentence, flagging any that don't. One small, single-purpose LLM node — not a separate topology.

This is a deliberate divergence from Anthropic's dedicated CitationAgent, which still lets the model *choose* which source to attach and so leaves misattribution possible. Binding by ID removes fabricated sources at the root; the verify pass catches the subtler "real source, wrong claim." The one-shot Writer is preserved — citation binding is a separate concern from prose generation, not a return to parallel section-writing.

**Invariant (load-bearing):** findings are **immutable and addressable by `finding_id`**. Any reduction of the finding set to fit the Writer's context (see *Open decisions*) may drop, reorder, or summarize-for-selection — but must never mutate a finding's `supporting_quote`, or both the ID resolution and the verify pass break. Only the *reduction algorithm* is open; this immutability is decided.

### Execution model: in-process fan-out (Pattern A)

Both topologies fan out **in-process**, running sub-agents inside the parent's run rather than spawning separate durable child tasks. A sub-agent is a subgraph that runs **inside the parent's run** — same `thread_id`, same checkpoint, same task row — fanned out either via LangGraph's `Send` map-reduce primitive or via gathered subgraph `ainvoke`s (the shape LangChain's Open Deep Research uses), and the parent's `ainvoke` drives the whole fan-out to completion before returning. This is the officially documented LangGraph approach to parallelism ([Send / graph API](https://docs.langchain.com/oss/python/langgraph/use-graph-api)). Verified against `langgraph==1.0.5`.

**What this buys (free, from LangGraph):**
- **Parallelism for I/O-bound sub-agents.** Our nodes are `async`; fanned sub-agents run concurrently on the event loop (web fetch + LLM calls overlap). The parent `await`s them but yields the loop — no CPU burn while waiting.
- **Crash durability as graph state.** LangGraph checkpoints at super-step boundaries and records per-node `pending_writes` (persisted in `pregel/_loop.py`, short-circuited on resume in `pregel/_runner.py`), so a worker crash mid-fan-out resumes the parent's run and re-executes only the unfinished sub-agents — completed siblings' results are restored, not recomputed. (LangGraph's *default* durability is `"async"`; this runtime invokes with `durability="sync"` — `executor/graph.py:3117` — persisting each super-step synchronously before the next, which is *stronger*, not weaker.)
- **One unified budget, lineage, and cancellation.** Sub-agent spend *is* the parent task's spend; cancelling the task cancels the run; there is one task row to observe and redrive.

**What this explicitly does *not* buy (the accepted Pattern-A tradeoffs):**
- **The parent holds its worker slot for the whole fan-out.** The parent task stays `running` (lease held, counting against Track 3's `max_concurrent_tasks`) from Scope through Writer — it does not suspend and release the slot while sub-agents work. The risk here is **slot exhaustion, not CPU** (async waiting isn't CPU-bound): a research agent with `max_concurrent_tasks = N` pins all N slots for multi-minute fan-outs, so an (N+1)-th task on that agent — even a quick one — can't be claimed until a fan-out frees a slot. Two consequences the design accepts: (1) fan-out topologies want a **lower concurrency default than chat** — the `research` preset defaults `max_concurrent_tasks` low (proposed **2**), not chat's 5; and (2) the binding capacity constraint is **worker-pool sizing** (total workers × fan-out duration), not the per-agent cap. *If this bites under multi-tenant load, the upgrade is durable cross-task sub-agents (each its own `thread_id`/task, parent suspends via `interrupt()`) — see Open decisions. Nothing here blocks that later.*
- **Single-sub-agent cancellation is gone.** There is no per-sub-agent task to `TaskStop`; an operator can only cancel the whole run. The per-sub-agent timeout/ceiling below is the substitute for a runaway branch.
- **Sub-agents are not independently addressable tasks.** No per-sub-agent task row, lease, dead-letter, or redrive; no `parent_task_id` task tree. Sub-agent activity is sub-steps in the parent's event timeline, and a partial sub-agent failure is handled in-graph (see *Partial subagent failure*), not as a separately dead-lettered task.

### Shared fan-out machinery

The in-process fan-out helper — run a ReAct sub-agent subgraph with isolated context + tool allowlist, return a structured summary — is the **same** for both topologies. **Built once, used by both.** The *driver* differs:

| | ReAct + `dispatch_subagent` (Topology 1) | Supervisor (Topology 2) |
|---|---|---|
| Who decides to fan out | The LLM, by emitting a tool call | The graph, by reading Supervisor's structured subtask list |
| Concurrency | LLM-emergent (depends on the model's multi-tool-call behavior) | Structural — the graph `Send`s N sub-agents in parallel, deterministically |
| Failure mode | LLM might not call the tool → no fan-out | Structurally guaranteed to fan out |
| Result injection | `ToolMessage` in the parent's message history | Typed state field (`state["subagent_results"]`) the Supervisor node reads |
| When this is the right driver | Coding/investigation agents that *sometimes* delegate — autonomy is the point | Deep Research, where the *guarantee* of structured fan-out is the point |

This is the same controllability argument that made Supervisor a topology rather than a tool, applied one level deeper. Share machinery, differ on driver.

**Per-sub-agent guardrails (enforced in the helper).** Because the whole fan-out is one super-step and Track 3 meters cost at the super-step boundary (the cost ledger is keyed per `checkpoint_id`), N multi-turn sub-agents can accrue spend *before* the meter next reads — so the per-task cap alone does not bound a single step. The helper closes this by enforcing, in graph state, a **per-sub-agent ceiling**: a token + turn cap (the `budget` arg on `dispatch_subagent`; a Supervisor default for structural fan-out). Worst-case overshoot of `budget_max_per_task` within a step is therefore bounded to `max_fanout × per-sub-agent-ceiling`, and operators size `budget_max_per_task` with that headroom. The same helper emits a **per-sub-agent heartbeat** event and enforces a **per-sub-agent timeout** — necessary because the parent task's lease stays healthy while it `await`s, so without a heartbeat a wedged branch is only inferable from silence. The turn cap also closes the depth-2 *width* question: a sub-agent's emergent `dispatch_subagent` count is bounded, not open-ended.

### Durability

Because fan-out is in-process (see *Execution model*), a Deep Research run is **one durable task**, not a tree of tasks. Its durability is LangGraph's per-task durability — the same machinery the runtime already uses for every ReAct task:

- **Crash recovery** is checkpoint-based: a worker dying mid-fan-out re-claims the parent task and resumes its run from the last super-step checkpoint, re-executing only the sub-agents whose `pending_writes` weren't persisted. No separate child tasks to reap.
- **Cancellation** cancels the one run; there is no cascade.
- **Lineage / observability** is the parent task's `task_events` timeline (Track 2), with sub-agent activity as sub-steps (see *Observability*).
- **Redrive** (`rollback_last_checkpoint`) rolls the parent run back to a prior super-step and re-runs forward.

**Resume-forward vs. rollback — they are different operations.** *Crash recovery* resumes **forward** from the last super-step checkpoint: completed sub-agents' `pending_writes` are restored and **not** recomputed; only unfinished branches re-run. *Operator redrive* (`rollback_last_checkpoint`) rolls **back** to a prior super-step, so that whole fan-out super-step re-runs and **does** recompute (new tokens — see *Budget and redrive*). For both to be coherent, the partial-result accumulator `subagent_results` is a **checkpointed reducer channel keyed by `subtask`**: successful findings *and* failure markers survive a crash idempotently, and a re-dispatched subtask updates its own entry instead of duplicating. *(A wide fan-out writes one large checkpoint per super-step — all sub-agents' accumulated state — a Postgres write/serialization pressure distinct from the Writer's context limit; see *Open decisions*.)*

This is a deliberate simplification over a durable-cross-task design (separate `thread_id`/task per sub-agent, `parent_task_id` tree, per-sub-agent lease/dead-letter). That design buys independent sub-agent addressability and worker-slot release, at the cost of building cross-task spawn-and-await orchestration on top of LangGraph. We don't take it on for v1; it's the documented upgrade path (see *Open decisions*).

### Budget and redrive

With in-process fan-out there is no cross-task tree to reconcile — budget and redrive are simply the **parent task's**:

- **Budget defers wholesale to Track 3 (Scheduler and Budgets).** The run is one task metered per `(tenant_id, agent_id)` against `budget_max_per_task` / `budget_max_per_hour`, evaluated at claim time and checkpoint boundaries ([track-3-scheduler-and-budgets.md](../phase-2/track-3-scheduler-and-budgets.md)). All sub-agent cost is just this task's cost — no rollup, no per-tree composition question. Over-budget work **pauses** (per-task → operator increase + manual resume; hourly → auto-recovers), Track 3's posture, with no special case.
- **Cost is cumulative and never refunded** — consistent with usage-based provider billing (Anthropic bills successful requests; a disconnect mid-successful-call is still charged — [billing policy](https://support.anthropic.com/en/articles/8114526-how-will-i-be-billed)). The cap, not a refund, is the protection. The only unbilled tokens are those the provider never billed us (e.g., a worker dies before the LLM call returns).
- **Redrive** (`rollback_last_checkpoint`) rolls the parent run back to a prior super-step and re-runs forward; a fan-out super-step re-runs all of its sub-agents (the super-step is the checkpoint/redrive unit). Re-execution costs real new tokens the meter counts; work before the rollback point is reused.

### Observability: one task, sub-agent sub-steps

A Deep Research run is **one task row**. Sub-agent activity — each sub-agent's tool calls and findings, grouped by iteration round — surfaces as sub-steps on Track 2's append-only `task_events` timeline, *not* as separate task rows. Events carry an in-state `iteration` (round) and `subtask` index so that a round-2 retry of a failed subtask links to its round-1 attempt (see *Partial subagent failure*) and the Console can render an expandable tree *within* the task (round → sub-agent → steps). No task-list explosion: 5 sub-agents × 2 rounds is still **1 task**, not 11.

### What customers configure

- Research query (the task input)
- Source allowlist (which web tools / document stores subagents may use)
- `max_fanout_per_iteration` and `max_iterations`
- Total budget envelope
- Writer output format (formal report vs. annotated bullet list)
- Whether Scope is allowed to ask clarifying questions (some customers will want headless operation)

## Workflow as a resource (not a topology)

**Workflow definition** is a first-class platform resource alongside Agent. Schema sketch:

```yaml
workflow_id: customer-onboarding-v3
version: 1
inputs:
  - name: customer_email
    type: string
steps:
  - id: fetch_crm_record
    tool: crm_lookup
    args: { email: "{{inputs.customer_email}}" }
  - id: verify_eligibility
    tool: eligibility_check
    args: { record: "{{steps.fetch_crm_record.output}}" }
    hitl_gate: approval_required_above_threshold   # optional
  - id: send_welcome
    tool: send_email
    args: { ... }
    retry_policy: { max: 3, backoff: exponential }
outputs:
  - name: status
    from: steps.send_welcome.output.status
```

Workflows are **deterministic in structure**: the runtime executes steps in declared order (or branches based on declared conditions), enforces per-step tool allowlists, and pauses on HITL gates. They are *not* topologies because they're not graphs of LLM nodes — they're declarative step lists with LLM-powered (or pure-tool) steps.

### Two ways a Workflow runs

1. **Invoked by a ReAct agent** via `execute_workflow`. The workflow runs as a child task of the agent; result returns to the agent's loop.
2. **Submitted directly** via `POST /v1/tasks` with `workflow_id` instead of `agent_id`. The workflow runs standalone — no LLM envelope. This is essentially **durable function execution with LLM-powered steps**: Temporal-shaped, but the platform provides the LLM call semantics for steps that need one. *(This is the Phase-3 shape. Until the Workflow track lands, a task targets an `agent_id` only — the polymorphic task target is not built now.)*

### Why this isn't an "agent mode"

Workflows are too prescriptive to call agents — the LLM doesn't choose what comes next. They're closer to durable workflow systems (Temporal, AWS Step Functions) than to autonomous agents. Customers reach for workflows when the work *is* a defined plan; they reach for agents when the work needs LLM judgment.

## Presets

Presets are **named JSON bundles of defaults** the customer applies at agent creation. They reduce the "blank-page" problem of configuring an agent from scratch.

| Preset | Topology | Notable defaults |
|---|---|---|
| `chat` | ReAct | Planning off, light compaction, customer-defined tools, small per-turn budget |
| `coding` | ReAct | Planning on (with platform preamble), coding primitives (Track 8) + sandbox, aggressive compaction (Track 7), `dispatch_subagent` enabled, larger per-task budget |
| `investigation` | ReAct | Planning on, broad tool allowlist (search, sandbox, BYOT), aggressive compaction, `dispatch_subagent` enabled |
| `research` | Supervisor | Web search + web fetch tool allowlist, default fan-out width of 5 subagents, low `max_concurrent_tasks` (proposed 2 — fan-out pins a slot for its whole duration, see *Execution model*), formal-report Writer style |
| `workflow_runner` | (none — runs Workflow directly) | Convenience preset that wires a workflow_id to a scheduled trigger |

Presets are *starting points*; customers override individual fields as needed. The platform owns the preset definitions but does not lock customers into them.

## How Planning Primitive composes

Three "plans" exist in the framing, and they are structurally different:

| Where it lives | Owner | Created | Mutable | What enforces it |
|---|---|---|---|---|
| **ReAct agent scratchpad** (the original Track 9) | Agent | Whenever agent calls `plan_write` | Yes, agent rewrites freely | Nothing — it's a self-reminder, injected post-compaction so it survives Track 7 |
| **Supervisor's research plan** | Supervisor node | Refined across iterations | Yes, Supervisor may add subtasks across iterations | The graph — subagents only run for plan entries the Supervisor delegates |
| **Workflow step list** | Workflow definition | Before execution | No (or only via HITL pause) | The runtime — agent cannot reorder steps |

**Decision:** the Planning Primitive (formerly Track 9) owns *only* the ReAct agent scratchpad. The Supervisor topology has its own internal plan field with a different schema (subtask → assigned-subagent-id, status, returned summary). The Workflow resource has its own step list. They share UX conventions in the Console (a list with progress indicators) but not data structures.

### What this resolves from the original Track 9 open-questions list

- **Ownership** (agent vs. customer vs. HITL): **Agent-only.** Customer-prescribed plans go through Workflow. HITL editing of in-flight plans is a Workflow concern (HITL gates per step).
- **HITL pause on item transitions**: **No.** Plan-tied HITL is the Workflow shape; ReAct + Track 9 plans stay silent w.r.t. HITL. Existing `waiting_for_input` / `waiting_for_approval` pause states handle pauses.
- **API surface (`GET` vs. `PATCH`)**: **Read-only `GET /v1/tasks/{id}/plan`**. Plan mutation is Workflow's surface.
- **Exactly-one-`in_progress` rule**: **Prompt-layer guidance in the preamble**, not tool-layer rejection. The plan isn't load-bearing; soft guidance suffices.

### What stays open for the Planning Primitive's own design pass

- Write semantics: full-list replace (Claude Code's `TodoWrite` shape) vs. patch ops
- Rendering format for injection (Markdown checkbox vs. JSON vs. compact list)
- Plan size limits (item count, content length, injection token budget)
- Console rendering composition with the unified activity timeline (the projection over checkpoints)
- Pre-compaction flush hook interaction with Track 7 (tentatively: no — the plan is durable + injected post-compaction)

## Patterns considered and explicitly not built

The brainstorm cataloged the full agent-pattern space before committing the cut. The patterns *not* shipped, and the reasoning:

| Pattern | Decision | Reason |
|---|---|---|
| **Plan-and-Execute (rigid)** | Not built | Same shape achievable with ReAct + Track 9 planning + "plan first" preamble. Real *enforcement* differs but no current customer ask justifies the build |
| **Plan-Execute-Replan** | Not built; name reserved | Promote to a topology if a customer says "the LLM still skips planning despite the preamble." Until then, ReAct + planning policy is sufficient |
| **Reflection** | Not a topology | A prompt preamble ("after producing your answer, critique it once, then revise") or a `self_critique` tool covers most cases. Topology-worthy only if the platform must *guarantee* reflection |
| **Reflexion** (cross-trial learning) | Not built | Niche — multi-trial workloads with episodic memory are not a current customer ask |
| **LLMCompiler / Graph Agents** (DAG with parallel execution) | Not built; name reserved | Real value for parallel-tool-throughput workloads; implementation cost is high. Phase 3+ candidate |
| **OpenAI deep-research style** (single agent + powerful reasoning model) | Not adopted | The advantage depends on a specific provider's training and is not platform-controllable. We prefer the orchestrator-worker pattern (Anthropic / LangChain) because the platform owns the shape |

## Implementation tracks that flow from this design

When prioritized, the following tracks become real (each gets its own design + plan):

| Track | Description | Status |
|---|---|---|
| **Planning Primitive** (was Track 9) | ReAct agent scratchpad plan + `plan_write` tool + injection. Scope narrowed by this framing | Pending — design ready in *How Planning Primitive composes* + open questions list |
| **Supervisor topology** (customer-facing: "Deep Research"; was Track 10) | Scope → Supervisor (with iteration) → parallel Subagents → Writer; in-process fan-out; per-sub-agent ceiling + heartbeat | Pending — design ready in *Topology 2: Supervisor* |
| **Workflow resource** | Definition schema; `execute_workflow` tool; direct submission API; HITL gates per step | Phase 3 candidate — new track, not on Phase 2 roadmap |
| **Presets** | Curated default bundles per use case | Small slice; can fold into Supervisor track or ship as a Phase 3 polish item |
| **`dispatch_subagent` tool** | In-process ReAct sub-agent (subgraph) available to ReAct agents | Small slice; can fold into Supervisor track since it shares the in-process fan-out machinery |

## Decisions log

- **2026-05-21**: Adopted "two topologies + delegation tools + Workflow resource + presets" framing. Removed Track 9 and Track 10 from Phase 2 track list — their concepts are subsumed here, and implementation work re-enters as tracks when prioritized with real plans.
- **2026-05-21**: Decided the agent-only ownership for ReAct planning; customer-prescribed plans go through Workflow.
- **2026-05-21**: Chose Supervisor-as-topology over orchestration-as-tool for controllability — the platform must own the fan-out structure rather than depend on the LLM choosing to use a dispatch tool.
- **2026-05-22**: Verified the Supervisor topology design against primary sources. Anthropic's pattern (Lead Researcher that plans + synthesizes + iterates + a separate CitationAgent) and LangChain's Open Deep Research (Scope → Supervisor → Subagents → one-shot Writer) were both reviewed. Final architecture: four-phase graph combining LangChain's Scope (with conditional clarification) + Supervisor (with iteration) + parallel Subagents + one-shot Writer. LangChain's empirical finding that parallel report-writing causes coordination problems is the load-bearing reason for one-shot Writer over Anthropic's Lead-also-synthesizes shape.
- **2026-05-22**: Two-layer naming committed. Customer-facing: "Deep Research"; internal: "Supervisor topology" (matches LangChain terminology; the academic name for the pattern is "orchestrator-worker"). *(The `agent_config.mode = "deep_research"` field originally proposed here was dropped on 2026-06-03 — see below; the naming itself stands.)*
- **2026-05-22**: Subagent execution machinery is shared between `dispatch_subagent` (Topology 1 tool) and Supervisor topology fan-out. Built once as a primitive; ReAct invokes via LLM tool call (loose, emergent), Supervisor invokes via structured graph-driven dispatch (strict, guaranteed). Subagent count in Supervisor is dynamic, decided per iteration by the Supervisor LLM within customer-configured `max_fanout_per_iteration` and `max_iterations` caps.
- **2026-05-22** *(superseded 2026-06-03 — see Pattern A below)*: An earlier draft made dispatched subagents first-class durable child tasks (own `parent_task_id`, lease, dead-letter, cascading cancellation, redrive composition), with parents pausing into a `waiting_for_subagent` state and resuming on child completion. This cross-task model was dropped in favor of in-process fan-out.
- **2026-05-22** *(superseded 2026-06-03)*: The accompanying "one Subagent Lifecycle Service + two thin adapters" decomposition (8 persistence concerns) assumed the cross-task model and no longer applies under in-process fan-out.
- **2026-06-03**: **Chose Pattern A — in-process fan-out — over Pattern B (durable cross-task sub-agents).** Both topologies fan out inside the parent's run (one `thread_id`, one task, one checkpoint) via `Send` or gathered subgraph `ainvoke`s, verified against `langgraph==1.0.5` — the in-process shape LangChain's Open Deep Research uses (it gathers subgraph invocations). Rationale: Pattern B's per-sub-agent task identity and worker-slot release require building cross-task spawn-and-await orchestration on top of LangGraph (not a native feature), which is large net-new work; Pattern A is officially supported and drops the entire cross-task persistence layer. Accepted tradeoffs: the parent holds its worker slot (counts against `max_concurrent_tasks`) for the whole fan-out — the risk is **slot exhaustion, not CPU** (async waiting isn't CPU-bound), mitigated by a low `research`-preset concurrency default and bounded by the fan-out/iteration caps — and sub-agents are in-graph sub-steps, not independently addressable/dead-letterable/cancellable tasks. Consequences: removed the `waiting_for_subagent` pause state, the `parent_task_id` task tree, budget rollup, and the multi-row Console tree; budget/redrive/cancellation collapse to the parent task's own (Track 3, unchanged). Pattern B remains the documented upgrade path if worker-slot pressure appears under multi-tenant load.
- **2026-06-03** (review refinements): tightened the Pattern-A cost-accounting the first cut glossed. (a) Budget overshoot — because a fan-out is one super-step and Track 3 meters at the checkpoint boundary, the shared fan-out helper enforces a **per-sub-agent token+turn ceiling** in graph state; worst-case per-step overshoot is `max_fanout × ceiling` and `budget_max_per_task` is sized with that headroom (this also bounds depth-2 width and replaces the vestigial per-sub-agent budget). (b) Liveness — the helper emits a **per-sub-agent heartbeat** and enforces a **per-sub-agent timeout**, since the parent's lease stays healthy while it awaits. (c) Recovery vs. redrive — *crash recovery resumes forward* (completed siblings not recomputed) vs. *operator redrive rolls back* (whole super-step recomputes); `subagent_results` is a checkpointed reducer keyed by `subtask` so partial results survive idempotently. (d) Single-sub-agent cancellation is an accepted loss (timeout/ceiling is the substitute). (e) Corrected the durability framing — the runtime invokes `durability="sync"` (`executor/graph.py:3117`), stronger than LangGraph's `"async"` default.
- **2026-06-03** (review pass): resolved five open points raised in design review.
  - **Citations** — bind deterministically: subagents emit `{finding_id, claim, source_url, supporting_quote}`, the Writer cites by `finding_id` (cannot fabricate a source), and a thin verification pass confirms each cited quote supports its sentence. Deliberate divergence from Anthropic's CitationAgent (which still lets the model choose the source). One-shot Writer preserved.
  - **Partial subagent failure** — Supervisor collects partial results and decides; the graph fails only when *zero* subagents return. One flaky subagent never sinks the run.
  - **Subagent recursion** — children are ReAct-only (never Supervisor); nesting bounded by `max_depth` (default 2). Budget bounds cost; depth needs its own structural cap.
  - **Config model** — dropped the proposed `agent_config.mode` field. Customer picks a **preset** → preset sets the internal **topology** field (`react` | `supervisor`) → topology fixes graph shape. "Deep Research" is the display label of the `research` preset. A task targets `agent_id`; the `workflow_id` task target is deferred to the Phase-3 Workflow track (schema not widened now).
  - **Budget / redrive** — actual tokens consumed, cumulative, never refunded (consistent with usage-based provider billing). Enforcement **defers to Track 3**: metered per `(tenant_id, agent_id)`, over-budget work **pauses** (per-task → manual resume; hourly → auto-recovers), *not* rejected — corrected from an earlier "reject" draft that contradicted Track 3's pause-over-dead-letter posture. With in-process fan-out (Pattern A) a run is one task, so all sub-agent cost is the parent task's own cost — the earlier "per-tree rollup / fan-out admission" open question dissolves. Redrive rolls the parent run back to a prior super-step and re-runs forward (rollback unit = the fan-out super-step); re-execution costs real new tokens, work before the rollback is reused. Only provider-unbilled tokens are free (e.g., worker dies pre-completion) — a metering consequence, not fault attribution.

## Open decisions

Genuinely unsettled; each belongs to the implementation track that picks it up.

- **One-shot Writer reduction *algorithm*.** The one-shot Writer solves a *coordination* problem but reintroduces a *context-size* one: many iterations × many subagents × findings all funnel into a single Writer call. When the corpus exceeds the Writer's context, how is it reduced — map-reduce summarization, Track 7 compaction on Writer input, or a hard cap on findings? The immutability *invariant* is already decided (see *Citation binding*: findings are never mutated, only selected/reordered, so `finding_id` resolution survives); only the reduction algorithm is open. Owner: Supervisor track.
- **`iteration` / `subtask` marker shape.** The *need* for both markers is decided (round marker + stable logical-subtask marker, in graph state / on events — see *Partial subagent failure* / *Observability*); the exact representation (state fields, event tagging) is a Supervisor-track detail.
- **Checkpoint payload size of a wide fan-out.** Each fan-out super-step persists one checkpoint holding all sub-agents' accumulated state/findings; with large fan-out × many findings this is a real Postgres write/serialization cost (distinct from the Writer-context limit above). Whether to cap per-step checkpoint size or stream findings to an offload store is a Supervisor-track concern. See *Durability*.
- **Finer in-flight budget metering.** The per-sub-agent ceiling bounds overshoot, but exact mid-fan-out metering (sub-agents are subgraphs with their own internal super-steps — whether the cost ledger should debit at those boundaries vs. only the parent's fan-out boundary) is a refinement deferred to the Supervisor track. See *Shared fan-out machinery*.
- **Pattern B as a future upgrade.** If the parent-holds-a-worker-slot tradeoff (see *Execution model*) becomes a capacity problem under multi-tenant load, the upgrade is durable cross-task sub-agents: each sub-agent its own `thread_id`/task with `parent_task_id` lineage, the parent suspending via `interrupt()` and resuming on child completion. This is application-level orchestration over LangGraph's durable-pause primitive (LangGraph has no native spawn-and-await), so it is deferred, not designed here. Not needed for v1.

## References

External (primary sources marked PRIMARY):
- [How we built our multi-agent research system | Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system) — **PRIMARY**. Anthropic's engineering blog on their Research feature. Source for: orchestrator-worker pattern with Lead Researcher + parallel Subagents + CitationAgent; the 90.2% improvement of an Opus-4-lead + Sonnet-4-subagent system over single-agent Claude Opus 4 on their internal research eval; LeadResearcher synthesizes results and decides whether more research is needed.
- [Open Deep Research | LangChain](https://www.langchain.com/blog/open-deep-research) — **PRIMARY**. LangChain's open-source three-phase deep-research architecture (Scope → Research → Write). Source for: User Clarification and Brief Generation nodes; the brief as "north star for success"; the empirical finding that parallel report-writing causes coordination problems and they switched to one-shot Writer.
- [Introducing deep research | OpenAI](https://openai.com/index/introducing-deep-research/) — **PRIMARY**. OpenAI's announcement. Source for the single-agent (o3-powered) approach we considered and did not adopt.
- [How OpenAI's Deep Research Works](https://blog.promptlayer.com/how-deep-research-works/) — third-party summary of OpenAI's approach (end-to-end RL training, single-context reasoning).
- [Plan-and-Execute Agents | LangChain](https://blog.langchain.com/planning-agents/) — plan-execute vs. ReAct tradeoffs.
- [LangGraph Supervisor Pattern](https://callsphere.ai/blog/langgraph-supervisor-multi-agent-orchestration-2026) — supervisor pattern in LangGraph; bottleneck and single-point-of-failure tradeoffs.
- [LangGraph Reflection & Reflexion](https://medium.com/towardsdev/built-with-langgraph-29-reflection-reflexion-10cc1cf96f35) — generate-critique-refine and episodic memory patterns (considered and not adopted as a topology).
- [An LLM Compiler for Parallel Function Calling (paper)](https://arxiv.org/pdf/2312.04511) — DAG-based parallel tool execution; 3.6× speedup claim (considered and reserved for Phase 3+).

Internal:
- `docs/design-docs/phase-2/design.md` — Phase 2 track structure (Tracks 1–8 remain; Tracks 9 and 10 removed per this framing)
- `docs/design-docs/phase-2/track-3-scheduler-and-budgets.md` — the budget/concurrency model this design's budget semantics defer to (`budget_max_per_task`, `budget_max_per_hour`, pause-not-fail, `(tenant_id, agent_id)` metering)
- `docs/design-docs/phase-2/track-7-context-window-management.md` — compaction transform that Planning Primitive's plan injection runs after
- `docs/design-docs/phase-2/track-8-coding-primitives.md` — the tool surface the `coding` preset bundles
- `docs/design-docs/phase-2/track-5-memory.md` — cross-task store (different scope from in-task plan)
- `docs/design-docs/core-beliefs.md` — architectural invariants
