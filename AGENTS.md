# AGENTS.md — Project Navigation

## Project
Cloud-Native Persistent Agent Runtime — durable execution for AI agents. See [ARCHITECTURE.md](./ARCHITECTURE.md) for key decisions and tech stack.

## Documentation Map
- `docs/product-specs/` — What the system should do (vision, user stories, core concepts)
- `docs/design-docs/` — How to build it (phase-based design documents)
  - `docs/design-docs/core-beliefs.md` — Key architectural invariants
  - `docs/design-docs/phase-N/design.md` — Primary design doc per phase
  - `docs/design-docs/langfuse/` — Standalone initiative design docs
  - `docs/design-docs/phase-3-plus/` — Forward-looking design notes
- `docs/exec-plans/` — Implementation plans
  - `docs/exec-plans/active/` — Plans currently being executed
  - `docs/exec-plans/completed/` — Archived completed plans
- `docs/references/` — External docs (placeholder, currently empty)
- `docs/generated/` — Auto-generated documentation (placeholder, currently empty)
- `docs/LOCAL_DEVELOPMENT.md` — Local setup and environment

## Services
- `services/api-service/` — Spring Boot REST API ([README](services/api-service/README.md))
- `services/console/` — React SPA ([README](services/console/README.md))
- `services/worker-service/` — Python worker ([README](services/worker-service/README.md))
- `services/model-discovery/` — Model discovery service

## New Phase Workflow

When starting a new phase (e.g., Phase 3), follow this order:

1. **Spec first** → Add Phase 3 sections to the existing files in `docs/product-specs/` (vision.md, user-stories.md, core-concepts.md). Tracks are subsections within those files.
2. **Design second** → Create `docs/design-docs/phase-3/design.md` as the primary design doc
3. **Plan third** → Create `docs/exec-plans/active/phase-3/` with plan.md, progress.md, and agent_tasks/
4. **Execute** → Implement per the task specs in agent_tasks/
5. **Archive** → When done, move `docs/exec-plans/active/phase-3/` → `docs/exec-plans/completed/phase-3/`
6. **Update status** → Update [STATUS.md](./STATUS.md) to reflect the phase/track state

### Tracks (chunking large phases)

When a phase contains too much work for a single planning cycle (e.g., 40+ tasks), split it into sequential tracks of ~7-10 tasks each. Tracks break a phase into manageable batches.

- **Spec**: Phase sections within the global files in `docs/product-specs/` — tracks are subsections
- **Design**: One `design.md` per phase as the overview. Add `track-N-<name>.md` alongside for track-specific design detail
- **Exec plans**: Each track gets its own subdirectory with plan.md, progress.md, and agent_tasks/
- **Archiving**: Move each track to `completed/` as it finishes. A phase is complete when all its tracks are archived.

Example (Phase 2 has 3 tracks):
- Spec: Phase 2 sections in `product-specs/vision.md`, `product-specs/user-stories.md`, etc.
- Design: `design-docs/phase-2/design.md` + `design-docs/phase-2/track-1-agent-control-plane.md` + `design-docs/phase-2/track-3-scheduler-and-budgets.md`
- Plans: `exec-plans/completed/phase-2/track-1/` · `exec-plans/completed/phase-2/track-2/` · `exec-plans/active/phase-2/track-3/`

## Current Status

See [STATUS.md](./STATUS.md) for phase-level tracking and links to each track's progress.

## Agent Skills (Superpowers)

**Non-negotiable:** If superpowers skills are installed, agents MUST use them.

1. **At conversation start**, invoke the `using-superpowers` skill via the `Skill` tool. This is mandatory before any other action — including reading files, exploring the codebase, or asking clarifying questions.
2. **Before every task**, check if a relevant skill applies (debugging, TDD, brainstorming, code review, etc.) and invoke it via the `Skill` tool. If there is even a 1% chance a skill is relevant, invoke it.
3. **Priority order**: User instructions > Superpowers skills > Default system behavior.
4. **Do not rationalize skipping skills.** "This is just a simple question" or "Let me explore first" are not valid reasons. The skill tells you *how* to explore or answer.

## Local Validation Notes

- For local testing, follow `README.md` and `docs/LOCAL_DEVELOPMENT.md`.
- The `Makefile` has wrapper targets for setup and testing (`make init`, `make install`, `make test`, `make start`, `make stop`, `make status`). Use these as the primary entry point.
- When validating background `Makefile` targets (`make start`, `make status`, `make stop`), prefer an interactive shell / PTY.
- **Python:** Always use the worker virtualenv at `services/worker-service/.venv/`. Run Python commands via `services/worker-service/.venv/bin/python` or activate with `source services/worker-service/.venv/bin/activate`. Do NOT use bare `python3` or `uv run` — the venv has all dependencies pinned.

## Testing (Mandatory)

**Every code change must be tested before it is considered done.** No exceptions. See [LOCAL_DEVELOPMENT.md](./docs/LOCAL_DEVELOPMENT.md) for full details on test locations, single-test commands, and conventions.

- **Write tests** for every code change. Cover all use cases and failure scenarios.
- `make test` — unit tests (fast, no infra). **Required after every change.**
- `make e2e-test` — E2E on isolated infra. **Required after DB/schema or cross-service changes.**
- `make test-all` — both combined.
- Run the **narrowest scope** that covers your change. If tests fail — including pre-existing failures — fix them before moving on.
