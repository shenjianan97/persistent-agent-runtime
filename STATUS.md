# Project Status

Bird's-eye view of where the project stands. For navigation and structure, see [AGENTS.md](./AGENTS.md).

## Phases

### Phase 1 — Durable Execution: Complete
- [Plan](docs/exec-plans/completed/phase-1/plan.md) · [Progress](docs/exec-plans/completed/phase-1/progress.md) — 9 tasks

### Phase 2 — Agent Runtime

| Track | Name | Status | Plan | Progress |
|-------|------|--------|------|----------|
| Track 1 | Agent Control Plane | Complete | [plan](docs/exec-plans/completed/phase-2/track-1/plan.md) | [progress](docs/exec-plans/completed/phase-2/track-1/progress.md) |
| Track 2 | HITL & Unified Timeline | Complete | [plan](docs/exec-plans/completed/phase-2/track-2/plan.md) | [progress](docs/exec-plans/completed/phase-2/track-2/progress.md) |
| Track 3 | Scheduler & Budgets | Complete | [plan](docs/exec-plans/completed/phase-2/track-3/plan.md) | [progress](docs/exec-plans/completed/phase-2/track-3/progress.md) |
| Track 4 | Custom Tool Runtime (BYOT) | Complete | [plan](docs/exec-plans/completed/phase-2/track-4/plan.md) | [progress](docs/exec-plans/completed/phase-2/track-4/progress.md) |
| Track 5 | Memory | Complete | [plan](docs/exec-plans/completed/phase-2/track-5/plan.md) | [progress](docs/exec-plans/completed/phase-2/track-5/progress.md) |
| Track 6 | GitHub Integration | Not started | — | — |
| Track 7 | Context Window Management | Complete | [plan](docs/exec-plans/completed/phase-2/track-7/plan.md) | [progress](docs/exec-plans/completed/phase-2/track-7/progress.md) |
| Track 8 | Coding-Agent Primitives | Design proposed ([track-8-coding-primitives.md](docs/design-docs/phase-2/track-8-coding-primitives.md)); relocated from AC Track 3; plan not started | — | — |

Tracks 9 (Planning Primitive) and 10 (Deep Research Mode) were removed on 2026-05-22 — their concepts are now subsumed by the cross-cutting [Agent Modes design](docs/design-docs/agent-modes/design.md). Implementation tracks that flow from that framing (Planning Primitive, Supervisor topology, Workflow resource, Presets) will be re-introduced with their own plans when prioritized.

### Cross-Cutting

| Initiative | Status | Design |
|------------|--------|--------|
| Langfuse Customer Integration | Complete | [plan](docs/exec-plans/completed/langfuse/plan.md) |
| Agent Capabilities (sandbox, artifacts, file input, coding primitives) | Tracks 1 & 2 complete; Track 3 proposed | [design](docs/design-docs/agent-capabilities/design.md) |
| Agent Modes (topologies, delegation tools, Workflow resource, presets) | Framing approved (2026-05-22); two tracks planned (2026-06-05) — see below | [design](docs/design-docs/agent-modes/design.md) · [tracks](docs/exec-plans/active/agent-modes/README.md) |

#### Agent Modes Tracks

| Track | Name | Status | Plan | Progress |
|-------|------|--------|------|----------|
| 1 | Supervisor Topology (Deep Research) | Planned (not started); blockers spiked & resolved; **3 deferred decisions (D1–D3) gated to S11/pre-GA — track not "done" until dispositioned ([§A12 ledger](docs/exec-plans/active/agent-modes/supervisor-topology/plan.md#a12-deferred-decisions-ledger-definition-of-done-gate))** | [plan](docs/exec-plans/active/agent-modes/supervisor-topology/plan.md) | [progress](docs/exec-plans/active/agent-modes/supervisor-topology/progress.md) |
| 2 | Planning Primitive | Planned (not started); low-risk, no open blockers | [plan](docs/exec-plans/active/agent-modes/planning-primitive/plan.md) | [progress](docs/exec-plans/active/agent-modes/planning-primitive/progress.md) |

Workflow resource and additional presets remain Phase-3 / deferred. The two planned tracks are independent and separately archivable. See the [tracks index](docs/exec-plans/active/agent-modes/README.md).

#### Agent Capabilities Tracks

| Track | Name | Status | Plan | Progress |
|-------|------|--------|------|----------|
| Track 1 | Output Artifact Storage | Complete | [plan](docs/exec-plans/completed/agent-capabilities/track-1/plan.md) | [progress](docs/exec-plans/completed/agent-capabilities/track-1/progress.md) |
| Track 2 | E2B Sandbox & File Input | Complete | [plan](docs/exec-plans/completed/agent-capabilities/track-2/plan.md) | [progress](docs/exec-plans/completed/agent-capabilities/track-2/progress.md) |
| Track 3 | Coding-Agent Primitives | Relocated to [Phase 2 Track 8](docs/design-docs/phase-2/track-8-coding-primitives.md) on 2026-04-18 | — | — |
