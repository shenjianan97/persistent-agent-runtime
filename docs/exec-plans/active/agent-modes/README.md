# Agent Modes — Implementation Tracks

Cross-cutting framing: [`docs/design-docs/agent-modes/design.md`](../../../design-docs/agent-modes/design.md).

The Agent Modes design spawns several implementation tracks. Two are planned here; each is **independent, separately reviewed, and separately archived**. The Workflow resource is explicitly Phase-3 and out of scope for both.

| Track | What it builds | Size / risk | Status |
|---|---|---|---|
| [Supervisor Topology](supervisor-topology/plan.md) | Deep Research multi-agent fan-out (Scope → Supervisor → Subagents → Writer), the shared in-process fan-out helper, the `dispatch_subagent` tool, and presets | 11 tasks; larger, carries the cost/fan-out blockers (see its §A11) | Planned — not started |
| [Planning Primitive](../../completed/agent-modes/planning-primitive/plan.md) | ReAct agent to-do-list scratchpad: `plan_write` tool + post-compaction injection + read-only `GET /v1/tasks/{id}/plan` + Console checklist | 5 tasks; small, low-risk | **Done & merged (#115, 2026-06-08)** — archived to [`completed/agent-modes/planning-primitive`](../../completed/agent-modes/planning-primitive/). `plan_write` shipped as a base platform tool (all agents); see that track's progress.md for follow-ups. |

**Why two tracks:** they come from the same design but are genuinely independent — different risk and size profiles, and only ~4 shared files (`RuntimeState`, the worker tool registry, the Console Activity pane, `types/index.ts`). They can be built in parallel; if so, worktree-isolate edits to the shared files (each track's plan §A3 *Cross-track coordination*).

**History:** these started as one combined plan (2026-06-05), then split into two tracks to match the design's own decomposition and the repo's per-track convention. The split preserved the four-lens review and the LangGraph 1.0.5 spike results — both captured in the Supervisor track's §A11.
