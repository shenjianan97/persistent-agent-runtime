# AGENTS.md — Project Navigation

## Project
Cloud-Native Persistent Agent Runtime — durable execution for AI agents. See [ARCHITECTURE.md](./ARCHITECTURE.md) for key decisions and tech stack.

## Documentation Map
- `docs/product-specs/` — What the system should do (vision, user stories, core concepts)
- `docs/design-docs/` — How to build it (phase-based design documents)
  - `docs/design-docs/core-beliefs.md` — Key architectural invariants
  - `docs/design-docs/phase-N/design.md` — Primary design doc per phase
  - `docs/design-docs/langfuse/` — Langfuse customer integration design
  - `docs/design-docs/agent-capabilities/` — Sandbox, artifacts, and file input design
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

### Task spec detail level

Task specs in `agent_tasks/` define **what** to build, not **how** to build it. They are contracts, not implementation blueprints.

**Include:** inputs, outputs, API contracts, schema changes, affected files, dependency graph, constraints (what NOT to do), existing code to reference as patterns, and acceptance criteria as observable behaviors.

**Do NOT include:** full source code, copy-paste SQL/Java/Python/TypeScript blocks, or line-by-line implementation. The implementing agent should read existing code, understand patterns, and write the implementation itself. Over-specified plans produce copy-paste work that misses integration bugs and becomes stale if the codebase evolves.
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
- Design: `design-docs/phase-2/design.md` + `design-docs/phase-2/track-1-agent-control-plane.md` + `design-docs/phase-2/track-3-scheduler-and-budgets.md` + `design-docs/phase-2/track-4-custom-tool-runtime.md`
- Cross-cutting: `design-docs/agent-capabilities/design.md` + `design-docs/langfuse/design.md`
- Plans: `exec-plans/completed/phase-2/track-1/` · `exec-plans/completed/phase-2/track-2/` · `exec-plans/completed/phase-2/track-3/` · `exec-plans/completed/phase-2/track-4/`

## Current Status

See [STATUS.md](./STATUS.md) for phase-level tracking and links to each track's progress.

## Agent Skills (Superpowers)

**Non-negotiable:** If superpowers skills are installed, agents MUST use them.

1. **At conversation start**, invoke the `using-superpowers` skill via the `Skill` tool. This is mandatory before any other action — including reading files, exploring the codebase, or asking clarifying questions.
2. **Before every task**, check if a relevant skill applies (debugging, TDD, brainstorming, code review, etc.) and invoke it via the `Skill` tool. If there is even a 1% chance a skill is relevant, invoke it.
3. **Priority order**: User instructions > Superpowers skills > Default system behavior.
4. **Do not rationalize skipping skills.** "This is just a simple question" or "Let me explore first" are not valid reasons. The skill tells you *how* to explore or answer.

## Parallel Subagent Safety

When orchestrating parallel subagents via the Agent tool, **always use `isolation: "worktree"`** if there is any chance two agents modify the same file — even different methods in the same file. Without worktrees, concurrent Edit tool calls on the same file can clobber each other (last writer wins, or `old_string` match fails silently).

- Before launching parallel agents, check "Affected Component / File paths" for overlap.
- If ANY file appears in both agents' scope, use `isolation: "worktree"` on at least one agent.
- After worktree agents complete, merge their branches into the main working tree.
- Only skip worktrees when agents have truly zero file overlap (e.g., Python worker vs React console).

### Browser verification is the orchestrator's job, not the subagent's

Parallel subagents **must not** run Playwright / `make start` browser verification themselves. `make start` binds global ports (`5173` Console, `8080` API, `55432`/`55433` Postgres) that are not namespaced per worktree, so two subagents racing `make start` will collide and both will observe flaky or false-negative behaviour — and even a single subagent can clobber a running dev stack the user or another agent started.

Ownership split when a task changes Console UI:

- **Subagent:** implement code, write/update unit tests (`make console-test`), update `CONSOLE_BROWSER_TESTING.md` with the new scenario, and commit. **Do not** call `make start`, `make stop`, or any Playwright MCP browser tool. Report the intended scenario text back to the orchestrator.
- **Orchestrator:** after merging the subagent's branch, start the stack once, run the Playwright scenarios described in `CONSOLE_BROWSER_TESTING.md` sequentially, and only mark the task done when browser verification passes. This is the "blocking gate" from §Browser Verification above — the gate lives with the orchestrator.

This applies to any long-lived port-binding workflow (`make start`, `make e2e-stack-up`, the dev-watch Vite server, etc.), not just Playwright. Subagents get unit tests and static checks; anything that needs the live stack runs once, serially, in the orchestrator.

## External Pull Request References

**Do not link to pull requests in other people's repositories** from commit messages or PR descriptions. GitHub creates a cross-reference timeline event on the target PR, which typically surfaces as a notification to its author — unsolicited noise once the upstream work has shipped.

Allowed references:
- PRs and commits in this repository
- PRs that *you* authored on any repository

When citing an upstream fix from an OSS dependency, refer to the released **version** that contains it (e.g., "`ddgs 9.12.1` replaced the shared executor with a per-call one") rather than the PR URL. The version pin in `pyproject.toml` / `build.gradle` / `package.json` is the technical guarantee; the URL is just provenance and can be dropped. If the *why* matters, summarize it inline in prose rather than linking out.

If an external PR reference slips in, rewrite the commit message and update the PR description before merge. Force-push is acceptable here — the rewrite is process hygiene, not content change.

## Local Validation Notes

- For local testing, follow `README.md` and `docs/LOCAL_DEVELOPMENT.md`.
- The `Makefile` has wrapper targets for setup and testing (`make init`, `make install`, `make test`, `make start`, `make stop`, `make status`). Use these as the primary entry point.
- When validating background `Makefile` targets (`make start`, `make status`, `make stop`), prefer an interactive shell / PTY.
- **Python:** Always use the worker virtualenv at `services/worker-service/.venv/`. Run Python commands via `services/worker-service/.venv/bin/python` or activate with `source services/worker-service/.venv/bin/activate`. Do NOT use bare `python3` or `uv run` — the venv has all dependencies pinned.
- **Test isolation:** All tests (worker integration, E2E) use a dedicated test database on port **55433** (`par-e2e-postgres`), never the local dev database on port 55432. This is enforced by `make worker-test` (passes `E2E_DB_DSN`) and `make e2e-test` (passes `E2E_DB_*` vars). Do NOT add tests that default to the dev DB — they will corrupt local development data.

## Testing (Mandatory)

**Every code change must be tested before it is considered done.** No exceptions. See [LOCAL_DEVELOPMENT.md](./docs/LOCAL_DEVELOPMENT.md) for full details on test locations, single-test commands, and conventions.

- **Write tests** for every code change. Cover all use cases and failure scenarios.
- `make test` — unit tests (fast, no infra). **Required after every change.**
- `make e2e-test` — E2E on isolated infra. **Required after DB/schema or cross-service changes.**
- `make test-all` — both combined.
- Run the **narrowest scope** that covers your change. If tests fail — including pre-existing failures — fix them before moving on.
- **CI maintenance:** When adding database migrations, new service containers, or infrastructure dependencies, verify `.github/workflows/ci.yml` picks them up. Migrations use a glob pattern (`[0-9][0-9][0-9][0-9]_*.sql`) so new migrations are auto-applied, but new services (e.g., LocalStack) must be added as CI service containers manually.

### Browser Verification (Console Changes) — BLOCKING

**Console changes are not done until verified in a real browser.** This is a blocking gate, not a suggestion. Unit tests with mocked data cannot catch cross-origin issues, encoding problems, stale data display, or broken download flows. Skip this and users will find the bugs instead.

After any change that affects the Console UI, verify it works using Playwright MCP tools (`browser_navigate`, `browser_snapshot`, `browser_click`, etc.). See [CONSOLE_BROWSER_TESTING.md](./docs/CONSOLE_BROWSER_TESTING.md) for standard scenarios and the scenario-selection matrix.

1. **Start the stack:** `make start` must be running (Console at `localhost:5173`, API at `localhost:8080`)
2. **Run Scenario 1** (Navigation Smoke Test) for all console changes
3. **Run the feature scenario** that covers the UI you changed — exercise the actual user flow end-to-end (submit data, wait for results, click buttons, verify downloads work)
4. **New features:** If your change adds UI not covered by existing scenarios, **add a new scenario** to the doc
5. **Mark done only after browser verification passes** — do not commit or create a PR with untested Console UI
