# Agent Modes — Supervisor Topology — Progress

Status tracking for the Supervisor Topology track ([plan.md](./plan.md)).
Sibling track: [Planning Primitive](../../../completed/agent-modes/planning-primitive/progress.md).
Statuses: `Not started` · `In progress` · `Done` · `Blocked`.

| Task | Description | Status | Note |
|---|---|---|---|
| S1 | API: `topology` (immutable) + `preset` + `supervisor` sub-object; validation; canonicalisation | Done | `SupervisorConfigRequest` + `AgentConfigRequest` fields + `ConfigValidationHelper` + `AgentService` immutability gate; 35 new `SupervisorConfigValidationTest` cases + 11 new `AgentServiceTest` cases; all 513 Java unit tests pass |
| S2 | `PresetDefaults` bundles applied at creation; `research` low-concurrency default | Not started | depends on S1 |
| S3 | Shared fan-out helper (`run_subagent`): isolated context + ceiling + heartbeat + timeout + depth cap | Not started | hard blocker for S4–S7 |
| S4 | `dispatch_subagent` ReAct tool wrapping the helper | Not started | depends on S3 |
| S5 | Supervisor Scope node: clarity + conditional clarify + immutable brief | Not started | depends on S3 |
| S6 | Supervisor node + structural `Send` fan-out + iteration + `subagent_results` reducer | Not started | depends on S5; riskiest task (E1/E2) |
| S7 | Subagent findings contract + one-shot Writer + citation binding + verify pass | Not started | depends on S6 |
| S8 | `_build_graph` topology branch + `durability="sync"` + cost-attribution mechanism | Not started | depends on S3..S7; riskiest task (E1/E2) |
| S9 | Migration `0025` + worker event emission + `ActivityProjectionService` tree mapping | Not started | migration must reach prod before S6/S8 emit |
| S10 | Console: preset selector (locked on edit) + supervisor section + sub-agent tree | Not started | depends on S1 |
| S11 | Supervisor E2E + Playwright scenario | Not started | depends on S1..S10 |

## Decisions / escalations (see plan §A11) — ALL RESOLVED (2026-06-05)

Settled via six throwaway spikes (`langgraph==1.0.5`, one against real Postgres) + concrete decisions:
- **E1 — cost mechanism PROVEN (spike #6):** `event["agent"]` misses sub-agent spend (baseline=0); `subgraphs=True` + aggregate captures all of it → S8 builds this.
- **E2 — RESOLVED (spikes #1/#5):** pause at the fan-out super-step boundary; durability holds (cross-process on Postgres).
- **E3 — DECIDED:** sub-agents are headless (no `interrupt()`); multi-interrupt resume not built.
- **E4 — DECIDED:** `subagent.heartbeat` → Langfuse span event (not a `task_events` row).
- **E5 — RESOLVED:** sub-agent transcript persists in the namespaced sub-checkpoint (spikes #2/#3); Console drill-in is a read path, no new table.
- **E6 — RESOLVED (ops):** per-agent `max_concurrent_tasks=2` + worker-pool sizing guidance; per-worker supervisor cap deferred.
- **E7 — DECIDED:** `research` preset `task_timeout_seconds=14400` (4 h, tunable).
- **E8 — DECIDED:** deterministic `subtask` ids (`{iteration}.{index}`) minted in S6.
- **E10 — DECIDED:** migration `0025` uses `ADD CONSTRAINT … NOT VALID` + `VALIDATE`.

**Durability proven (spikes #2–#5):** sub-agent = checkpointed subgraph node via `Send`; per-inner-turn crash resume, dynamic-N fan-out, transcript persistence, and `dispatch_subagent`-via-`Send` threading — all verified, including cross-process on real Postgres.

## Definition of done (track completion gate)

This track is **not "complete" / archivable** until **both** hold:
1. All tasks S1–S11 done (table above), and
2. The **Deferred Decisions Ledger** (plan [§A12](./plan.md#a12-deferred-decisions-ledger-definition-of-done-gate)) is **fully dispositioned** — every row (D1 checkpoint-size, D2 Writer reduction, D3 in-flight metering) is either **Closed** (with the metric as evidence) or **spun into a named follow-up task**. The S11 acceptance criteria force this look during E2E testing.

"v1 shipped" ≠ "fully optimized" — do not mark this track done while any §A12 row is OPEN.

**Deferred decisions outstanding:** D1, D2, D3 — all **OPEN** (review gate: S11 acceptance → research pre-GA).
