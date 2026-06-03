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
│  ├── dispatch_subagent  (ad-hoc child agent task)            │
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

Spawns a child task as a focused ReAct agent with its own context window and tool allowlist. Returns a structured summary to the parent on completion. The child is a normal durable task — checkpointed, observable, dead-letter-able, budget-tracked.

The child is always a **ReAct** agent — children are never themselves Supervisor topology. Nesting is bounded by a `max_depth` cap (default **2**): budget caps bound *cost*, but depth needs its own structural limit, or a buggy prompt can recurse into an expensive fork-bomb before the budget trips. **Depth is the `parent_task_id`-chain length, counted regardless of topology** — a Supervisor's structural fan-out consumes one level just as a `dispatch_subagent` call does, so a Supervisor → ReAct-subagent → `dispatch_subagent` chain reaches the depth-2 ceiling. This keeps the fork-bomb bound unambiguous across both topologies.

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
- **Per-iteration parallel fan-out**: the graph reads the Supervisor's subtask list and dispatches N children in parallel (see *Shared subagent machinery* below)
- **Budget rollup**: parent task's budget envelope includes all subagent costs
- **Fan-out and iteration caps**: hard limits on subagents-per-iteration and total Supervisor → Subagents loops to prevent runaway

### Subagent count — dynamic, capped

Subagent count is **not fixed**. The Supervisor decides per iteration how many subtasks to emit, based on how it decomposes the brief. Both primary sources confirm this is the right model: Anthropic's blog notes their early Lead Researchers *"made errors like spawning 50 subagents for simple queries"* before they tuned the decomposition, and LangChain's Supervisor delegates to "an appropriate number of sub-agents" decided dynamically. Our customer-configurable caps (`max_fanout_per_iteration`, `max_iterations`) bound the cost envelope; the LLM picks the actual count within those bounds.

### Partial subagent failure

A subagent that fails (error, budget exhaustion, cancellation) does **not** error the whole graph. The Supervisor node receives the partial result set — successful subagents' findings plus a failure marker for each that didn't return — and decides how to proceed: re-dispatch the failed subtask next iteration, proceed with what it has, or, **only if zero subagents returned**, fail the task. Fail-fast applies to the all-failed case alone; one flaky web fetch must not sink an otherwise-complete research run.

Re-dispatching across rounds requires a stable **`subtask_id`** distinct from `iteration_id` (the round marker): the round-2 retry of a failed subtask must link back to its round-1 attempt, or the Console tree renders them as two unrelated children instead of "subtask X: failed (round 1) → succeeded (round 2)." So child tasks carry both `iteration_id` (which round) and `subtask_id` (which logical subtask, surviving re-dispatch). See *iteration rounds* and *Open decisions*.

### Citation binding

Citations are bound deterministically rather than left to the Writer's discretion:

1. **Subagents emit structured findings**, each `{finding_id, claim, source_url, supporting_quote}` — not freeform prose with inline links.
2. **The Writer cites by `finding_id` only.** It cannot invent a source: an ID either resolves to a real finding or it doesn't. The runtime resolves IDs → full citations at render time, with no LLM involvement.
3. **A thin verification pass** takes each cited sentence plus its referenced finding's quote and confirms the quote actually supports the sentence, flagging any that don't. One small, single-purpose LLM node — not a separate topology.

This is a deliberate divergence from Anthropic's dedicated CitationAgent, which still lets the model *choose* which source to attach and so leaves misattribution possible. Binding by ID removes fabricated sources at the root; the verify pass catches the subtler "real source, wrong claim." The one-shot Writer is preserved — citation binding is a separate concern from prose generation, not a return to parallel section-writing.

**Invariant (load-bearing):** findings are **immutable and addressable by `finding_id`**. Any reduction of the finding set to fit the Writer's context (see *Open decisions*) may drop, reorder, or summarize-for-selection — but must never mutate a finding's `supporting_quote`, or both the ID resolution and the verify pass break. Only the *reduction algorithm* is open; this immutability is decided.

### Shared subagent machinery

The underlying primitive for spawning a child agent is the **same** as `dispatch_subagent`: child task creation with isolated context, isolated tool allowlist, budget rolled up to parent, structured summary returned. **Built once, used by both topologies.**

The *driver* differs:

| | ReAct + `dispatch_subagent` (Topology 1) | Supervisor (Topology 2) |
|---|---|---|
| Who decides to dispatch | The LLM, by emitting a tool call | The graph, by reading Supervisor's structured subtask list |
| Concurrency | LLM-emergent (depends on multi-tool-call behavior of the model) | Structural — graph fans out N children in parallel, deterministically |
| Failure mode | LLM might not call the tool → no fan-out | Structurally guaranteed to fan out, or the graph errors |
| When this is the right driver | Coding/investigation agents that *sometimes* benefit from delegation — autonomy is the point | Deep Research, where the *guarantee* of structured fan-out is the point |

This is the same controllability argument that made Supervisor a topology rather than a tool, applied one level deeper. Share machinery, differ on driver.

### Persistence

A dispatched subagent is a **first-class durable child task** with the same persistence guarantees as the parent — consistent with the platform's "durable execution" premise.

Decomposing persistence into its concerns shows that **the expensive parts are 100% shared between the two topologies, and the parts that differ are thin LangGraph-integration adapters**:

| # | Concern | Topology 1 (tool) | Topology 2 (graph) | Shared? |
|---|---|---|---|---|
| 1 | Create child task with `parent_task_id` | Same | Same | ✅ Identical |
| 2 | Child lease / checkpoint / crash recovery | Phase 1 machinery | Phase 1 machinery | ✅ Identical |
| 3 | Lineage tracking (`parent_task_id` FK) | Same FK | Same FK | ✅ Identical |
| 4 | Budget rollup (incremental debit) | Same | Same | ✅ Identical |
| 5 | Cascading cancellation | Same | Same | ✅ Identical |
| 6 | Redrive composition with `rollback_last_checkpoint` | Same | Same | ✅ Identical |
| 7 | Pause parent at dispatch | Tool-execution layer pauses (extends Track 2's `waiting_for_input` pause mechanism) | Graph-fan-out edge pauses (same primitive, different call site) | ⚠️ Same primitive, different call site |
| 8 | Inject child results on resume | Results arrive as `ToolMessage` entries in the parent's message history | Results land in a typed state field (`state["subagent_results"]`) that the Supervisor node reads | ⚠️ Different integration shape |

**Reuse vs. new build:** "✅ Identical" means identical *between the two topologies* — **not already implemented**. Phase 1 ships the single-task lease/checkpoint/recovery primitive (concern #2). The parent/child layer on top of it — `parent_task_id` lineage, budget rollup, cancellation cascade, redrive composition — is net-new build *shared by both topologies*, not existing machinery being wired up. (`parent_task_id` does not yet exist anywhere in the schema.)

#### Architectural shape: one shared service + two thin adapters

```
┌─────────────────────────────────────────────────────────────┐
│                  Subagent Lifecycle Service                 │
│  (one implementation, used by both topologies)              │
│  - Spawn child task with parent_task_id                     │
│  - Lease / checkpoint / crash-recovery (Phase 1 machinery)  │
│  - Budget rollup, cancellation cascade, redrive composition │
│  - Pause parent (checkpoint + status + release lease)       │
│  - Detect child completion → enqueue parent resume          │
└──────────┬──────────────────────────────────────────────────┘
           │
   ┌───────┴────────┐
   ▼                ▼
┌──────────────┐  ┌──────────────────┐
│ Tool adapter │  │ Graph adapter    │
│ (Topology 1) │  │ (Topology 2)     │
│              │  │                  │
│ Invoke via   │  │ Invoke via       │
│ tool call    │  │ graph fan-out    │
│              │  │ node             │
│ Inject on    │  │ Inject on        │
│ resume:      │  │ resume:          │
│ ToolMessage  │  │ state field      │
└──────────────┘  └──────────────────┘
```

The adapters are thin (each handles only "how to invoke the spawn service from the parent's graph" and "how to inject the child's result back into the parent's state on resume"). All six expensive concerns — durable spawn, lease/checkpoint/recovery, budget, cancellation, redrive, lineage — are built once in the shared service.

The thin-adapter shape is *evidence the boundary is well-placed*. If an adapter needed to reimplement budget rollup or lease management, that would signal the boundary was wrong.

#### What customers see (same in both topologies)

- Child tasks survive worker crashes (lease reaper + checkpointer)
- Parents do not burn a worker slot while waiting on children (paused + lease released)
- Lineage is observable: parent ↔ children via `parent_task_id`; events on Track 2's append-only `task_events` timeline
- Budget tracking is durable and incremental — interrupted subagents preserve costs already incurred
- Cancelling the parent cancels in-flight children; cancelling a child bubbles up as an error the parent decides how to handle
- Redrive past a completed subagent rolls the subagent's result back along with the parent

#### Budget and redrive semantics

Budget is **actual tokens consumed, cumulative, and never refunded** — consistent with usage-based provider billing (Anthropic bills only successful requests, but a disconnect mid-successful-call is still charged — [billing policy](https://support.anthropic.com/en/articles/8114526-how-will-i-be-billed)). You pay for the tokens a malfunctioning or retried agent burns; the *cap*, not a refund, is the protection. Enforcement defers to **Track 3 (Scheduler and Budgets)** rather than inventing a parallel mechanism:

- Budget is metered globally per `(tenant_id, agent_id)` via Track 3's `budget_max_per_task` and `budget_max_per_hour`, evaluated at claim time and checkpoint boundaries ([track-3-scheduler-and-budgets.md](../phase-2/track-3-scheduler-and-budgets.md)). Subagents are tasks under the same `agent_id`, so their spend already counts against the agent meter automatically.
- Over-budget work **pauses, it is not rejected** (Track 3 chose pause over dead-lettering so expensive work isn't lost): per-task exhaustion pauses and requires an operator budget increase + manual resume; hourly exhaustion pauses and auto-recovers as the rolling window clears. A redrive whose re-execution would cross a budget therefore **pauses at its next boundary** — same mechanism, no special case.
- A redrive (`rollback_last_checkpoint`) re-runs the work after the rollback point, costing **real new tokens** that the meter counts; work *before* the rollback point is reused (durable-execution replay: completed work is never redone).
- The only tokens not charged are those the provider never charged *us* for — e.g., a worker dies before the LLM call returns, so no completion was billed. This falls out of metering actual provider usage; it is **not** a fault-attribution policy (we do not distinguish "system fault" vs. "agent fault" for billing).
- Re-running children on redrive creates **new child task records** (append-only lineage); the superseded round stays visible. Never in-place mutation.

**Redrive granularity across a parallel fan-out:** the rollback unit is the whole fan-out **super-step**. Redriving past a round where 8/10 children succeeded re-runs all 10 and re-spends — the simple, consistent choice that matches "the super-step is the checkpoint boundary." Per-child reuse of the 8 successful findings is a possible later optimization, not the v1 contract.

**Open (Supervisor track):** how `budget_max_per_task` composes across a parent + children *tree* (Track 3 predates subagent trees), and whether the Supervisor adds pre-fan-out admission control — Track 3 enforcement is reactive (claim-time + checkpoint only, no predictive admission), so N parallel children *can* overshoot `budget_max_per_hour` before their checkpoints fire. Accepted as a known reactive-enforcement property unless the Supervisor track adds reservation.

#### Topology 2 nuance: iteration rounds

Because the Supervisor may iterate (dispatch round 1, see results, dispatch round 2), a single parent task can accumulate multiple *rounds* of children. Children need to be distinguishable per round *and* per logical subtask for observability, partial retries, and Console rendering. Solution: child tasks carry both an `iteration_id` (round) and a `subtask_id` (logical subtask, stable across re-dispatch) alongside `parent_task_id`, populated by the graph adapter — `iteration_id` alone cannot link a round-2 retry to its round-1 failure (see *Partial subagent failure*). *Exact column shapes are an implementation-track detail.*

#### UX implication (flag for the implementation track)

Child tasks will be visible in the Console's task list. A Deep Research task with 5 subagents per round across 2 rounds will produce 11 task records (1 parent + 10 children). The implementation needs tree-under-parent rendering — not flat siblings — to keep this comprehensible.

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
| `research` | Supervisor | Web search + web fetch tool allowlist, default fan-out width of 5 subagents, formal-report Writer style |
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
| **Supervisor topology** (customer-facing: "Deep Research"; was Track 10) | Scope → Supervisor (with iteration) → parallel Subagents → Writer; parallel subagent execution; budget rollup | Pending — design ready in *Topology 2: Supervisor* |
| **Workflow resource** | Definition schema; `execute_workflow` tool; direct submission API; HITL gates per step | Phase 3 candidate — new track, not on Phase 2 roadmap |
| **Presets** | Curated default bundles per use case | Small slice; can fold into Supervisor track or ship as a Phase 3 polish item |
| **`dispatch_subagent` tool** | Child-task spawning available to ReAct agents | Small slice; can fold into Supervisor track since it shares execution machinery |

## Decisions log

- **2026-05-21**: Adopted "two topologies + delegation tools + Workflow resource + presets" framing. Removed Track 9 and Track 10 from Phase 2 track list — their concepts are subsumed here, and implementation work re-enters as tracks when prioritized with real plans.
- **2026-05-21**: Decided the agent-only ownership for ReAct planning; customer-prescribed plans go through Workflow.
- **2026-05-21**: Chose Supervisor-as-topology over orchestration-as-tool for controllability — the platform must own the fan-out structure rather than depend on the LLM choosing to use a dispatch tool.
- **2026-05-22**: Verified the Supervisor topology design against primary sources. Anthropic's pattern (Lead Researcher that plans + synthesizes + iterates + a separate CitationAgent) and LangChain's Open Deep Research (Scope → Supervisor → Subagents → one-shot Writer) were both reviewed. Final architecture: four-phase graph combining LangChain's Scope (with conditional clarification) + Supervisor (with iteration) + parallel Subagents + one-shot Writer. LangChain's empirical finding that parallel report-writing causes coordination problems is the load-bearing reason for one-shot Writer over Anthropic's Lead-also-synthesizes shape.
- **2026-05-22**: Two-layer naming committed. Customer-facing: "Deep Research"; internal: "Supervisor topology" (matches LangChain terminology; the academic name for the pattern is "orchestrator-worker"). *(The `agent_config.mode = "deep_research"` field originally proposed here was dropped on 2026-06-03 — see below; the naming itself stands.)*
- **2026-05-22**: Subagent execution machinery is shared between `dispatch_subagent` (Topology 1 tool) and Supervisor topology fan-out. Built once as a primitive; ReAct invokes via LLM tool call (loose, emergent), Supervisor invokes via structured graph-driven dispatch (strict, guaranteed). Subagent count in Supervisor is dynamic, decided per iteration by the Supervisor LLM within customer-configured `max_fanout_per_iteration` and `max_iterations` caps.
- **2026-05-22**: Dispatched subagents are first-class durable child tasks. Same persistence guarantees as parent tasks (crash recovery via lease reaper + checkpointer, durable budget tracking, cascading cancellation, redrive composition, observable lineage via `parent_task_id`). Parents pause at dispatch and resume on child completion, reusing Track 2's pause infrastructure — they do not burn worker slots while waiting.
- **2026-05-22**: Decomposed persistence to confirm sharing intent is well-founded. 6 of 8 concerns (spawn, lease/checkpoint/recovery, lineage, budget, cancellation, redrive) are 100% identical between topologies and live in a single Subagent Lifecycle Service. The 2 remaining concerns (parent pause and child-result injection) share a pause primitive but differ in LangGraph integration shape — each topology has a thin adapter (~tool-call vs. graph-fan-out) that invokes the shared service. The thinness of the adapters is evidence the architectural boundary is well-placed.
- **2026-06-03** (review pass): resolved five open points raised in design review.
  - **Citations** — bind deterministically: subagents emit `{finding_id, claim, source_url, supporting_quote}`, the Writer cites by `finding_id` (cannot fabricate a source), and a thin verification pass confirms each cited quote supports its sentence. Deliberate divergence from Anthropic's CitationAgent (which still lets the model choose the source). One-shot Writer preserved.
  - **Partial subagent failure** — Supervisor collects partial results and decides; the graph fails only when *zero* subagents return. One flaky subagent never sinks the run.
  - **Subagent recursion** — children are ReAct-only (never Supervisor); nesting bounded by `max_depth` (default 2). Budget bounds cost; depth needs its own structural cap.
  - **Config model** — dropped the proposed `agent_config.mode` field. Customer picks a **preset** → preset sets the internal **topology** field (`react` | `supervisor`) → topology fixes graph shape. "Deep Research" is the display label of the `research` preset. A task targets `agent_id`; the `workflow_id` task target is deferred to the Phase-3 Workflow track (schema not widened now).
  - **Budget / redrive** — actual tokens consumed, cumulative, never refunded (consistent with usage-based provider billing). Enforcement **defers to Track 3**: metered per `(tenant_id, agent_id)`, over-budget work **pauses** (per-task → manual resume; hourly → auto-recovers), *not* rejected — corrected from an earlier "reject" draft that contradicted Track 3's pause-over-dead-letter posture. Redrive re-runs post-rollback work at real new cost (rollback unit = the fan-out super-step); work before the rollback is reused. Only provider-unbilled tokens are free (e.g., worker dies pre-completion) — a metering consequence, not fault attribution. Re-run children are new append-only records. *Open:* per-tree `budget_max_per_task` composition and pre-fan-out admission control (Track 3 is reactive-only).

## Open decisions

Genuinely unsettled; each belongs to the implementation track that picks it up.

- **One-shot Writer reduction *algorithm*.** The one-shot Writer solves a *coordination* problem but reintroduces a *context-size* one: many iterations × many subagents × findings all funnel into a single Writer call. When the corpus exceeds the Writer's context, how is it reduced — map-reduce summarization, Track 7 compaction on Writer input, or a hard cap on findings? The immutability *invariant* is already decided (see *Citation binding*: findings are never mutated, only selected/reordered, so `finding_id` resolution survives); only the reduction algorithm is open. Owner: Supervisor track.
- **`iteration_id` / `subtask_id` column shapes.** The *need* for both fields is decided (round marker + stable logical-subtask marker, see *Partial subagent failure* / *iteration rounds*); the exact schema (types, indexing, FK shape) is a Supervisor-track detail.
- **Per-tree budget composition + fan-out admission.** How Track 3's per-task budget composes across a parent + children tree, and whether the Supervisor adds pre-fan-out budget reservation (Track 3 enforcement is reactive-only, so parallel children can overshoot the hourly cap before checkpoints fire). Owner: Supervisor track. See *Budget and redrive semantics*.

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
