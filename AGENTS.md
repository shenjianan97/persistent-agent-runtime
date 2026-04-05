# AGENTS.md — Project Navigation

## Project
Cloud-Native Persistent Agent Runtime — durable execution for AI agents. See [ARCHITECTURE.md](./ARCHITECTURE.md) for key decisions and tech stack.

## Documentation Map
- `docs/product-specs/` — What the system should do (vision, user stories, core concepts)
- `docs/design-docs/` — How to build it (phase-based design documents)
  - `docs/design-docs/core-beliefs.md` — Key architectural invariants
  - `docs/design-docs/phase-N/design.md` — Primary design doc per phase
- `docs/exec-plans/` — Implementation plans
  - `docs/exec-plans/active/` — Plans currently being executed
  - `docs/exec-plans/completed/` — Archived completed plans
- `docs/references/` — External docs, llms.txt files
- `docs/generated/` — Auto-generated documentation
- `docs/LOCAL_DEVELOPMENT.md` — Local setup and environment

## Services
- `services/api-service/` — Spring Boot REST API ([README](services/api-service/README.md))
- `services/console/` — React SPA ([README](services/console/README.md))
- `services/worker-service/` — Python worker ([README](services/worker-service/README.md))

## New Phase Workflow

When starting a new phase (e.g., Phase 3), follow this order:

1. **Spec first** → Create `docs/product-specs/phase-3/spec.md` with feature specs, user stories, acceptance criteria (tracks are sections within the file)
2. **Design second** → Create `docs/design-docs/phase-3/design.md` as the primary design doc
3. **Plan third** → Create `docs/exec-plans/active/phase-3/` with plan.md, progress.md, and agent_tasks/
4. **Execute** → Implement per the task specs in agent_tasks/
5. **Archive** → When done, move `docs/exec-plans/active/phase-3/` → `docs/exec-plans/completed/phase-3/`

### Tracks (chunking large phases)

When a phase contains too much work for a single planning cycle (e.g., 40+ tasks), split it into sequential tracks of ~7-10 tasks each. Tracks break a phase into manageable batches.

- **Spec**: One `spec.md` per phase — tracks are sections within the same file
- **Design**: One `design.md` per phase as the overview. Add `track-N-<name>.md` alongside for track-specific design detail
- **Exec plans**: Each track gets its own subdirectory with plan.md, progress.md, and agent_tasks/
- **Archiving**: Move each track to `completed/` as it finishes. A phase is complete when all its tracks are archived.

Example (Phase 2 had 2 tracks, ~14 tasks total):
- Spec: `product-specs/phase-2/spec.md`
- Design: `design-docs/phase-2/design.md` + `design-docs/phase-2/track-1-agent-control-plane.md`
- Plans: `exec-plans/completed/phase-2/track-1/` (7 tasks) and `exec-plans/completed/phase-2/track-2/` (7 tasks)

## Current Status

- Phase 1 (Durable Execution): Complete
- Phase 2 Track 1 (Agent Control Plane): Complete
- Phase 2 Track 2 (HITL & Unified Timeline): Complete
- Langfuse Customer Integration: Complete
- Stage 5 (Validation): In progress — remaining: crash-recovery demo, perf testing, demo video
- Stage 6 (Launch): Not started

## Agent Skills (Superpowers)

If superpowers skills are installed, agents MUST use them. Before starting any task, check if a relevant skill applies and invoke it via the `Skill` tool. Skills provide specialized workflows (debugging, TDD, brainstorming, code review, etc.) that override default behavior. User instructions always take precedence over skills.

## Local Validation Notes

- For local testing, follow `README.md` and `docs/LOCAL_DEVELOPMENT.md`.
- When validating background `Makefile` targets (`make start`, `make status`, `make stop`), prefer an interactive shell / PTY.
