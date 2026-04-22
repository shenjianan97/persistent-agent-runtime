# AGENTS.md — Project Navigation

## Project
Cloud-Native Persistent Agent Runtime — durable execution for AI agents.

**Stack:** Java + Spring Boot (API) · Python (worker) · TypeScript + React 19 + Vite (Console) · PostgreSQL · LangGraph. See [ARCHITECTURE.md](./ARCHITECTURE.md) for full stack and rationale.

## Documentation Map
- `docs/product-specs/` — What the system should do (vision, user stories, core concepts)
- `docs/design-docs/` — How to build it
  - `core-beliefs.md` — Architectural invariants
  - `phase-N/design.md` — Primary design doc per phase
  - `langfuse/`, `agent-capabilities/`, `phase-3-plus/` — Cross-cutting and forward-looking
- `docs/exec-plans/active/` and `docs/exec-plans/completed/` — Implementation plans
- `docs/LOCAL_DEVELOPMENT.md` — Local setup and environment

## Services
- `services/api-service/` — Spring Boot REST API ([README](services/api-service/README.md))
- `services/console/` — React SPA ([README](services/console/README.md))
- `services/worker-service/` — Python worker ([README](services/worker-service/README.md))
- `services/model-discovery/` — Model discovery service

## Common Commands

```bash
make help        # list all targets with descriptions
make init        # first-time setup
make install     # install deps across services
make test        # unit tests (fast, no infra)
make e2e-test    # E2E on isolated infra (DB port 55433)
make test-all    # unit + E2E
make start       # live stack: Console :5173, API :8080
make stop        # stop live stack
make status      # background-target status

# Python (worker) — always use the pinned venv:
services/worker-service/.venv/bin/python ...
```

## New Phase Workflow

1. **Spec** → Add phase sections to `docs/product-specs/` (vision.md, user-stories.md, core-concepts.md)
2. **Design** → Create `docs/design-docs/phase-N/design.md`
3. **Plan** → Create `docs/exec-plans/active/phase-N/` with plan.md, progress.md, agent_tasks/
4. **Execute** → Implement per the task specs
5. **Archive** → Move `active/phase-N/` → `completed/phase-N/`
6. **Update status** → Update [STATUS.md](./STATUS.md)

### Task spec detail level

Task specs in `agent_tasks/` are contracts, not implementation blueprints. They define **what**, not **how**.

**Include:** inputs, outputs, API contracts, schema changes, affected files, dependency graph, constraints, existing code to reference as patterns, acceptance criteria as observable behaviors.

**Exclude:** full source code or paste-ready SQL/Java/Python/TypeScript. The implementing agent reads existing code and writes the implementation itself — over-specified plans produce copy-paste work that misses integration bugs.

### Tracks (chunking large phases)

When a phase exceeds ~40 tasks, split into sequential tracks of ~7-10 tasks each. Tracks are spec subsections, may add `track-N-<name>.md` design docs, and each get their own `exec-plans/` subdirectory. Archive per-track; a phase is complete when all tracks are archived. See `exec-plans/completed/phase-2/` for a worked example.

## Agent Skills (Superpowers)

**Non-negotiable when installed.** At conversation start, invoke `using-superpowers` via the `Skill` tool before any other action — including reading files or asking clarifying questions. Before every task, invoke any relevant skill (debugging, TDD, brainstorming, code review). If there's even a 1% chance a skill applies, invoke it. "This is a simple question" and "let me explore first" are not valid reasons to skip — the skill tells you *how* to explore.

Priority: user instructions > skills > default behavior.

## Claims Require Evidence

**Never state a fact or recommend a practice from memory alone.** Every non-trivial claim — "this is the idiomatic pattern", "library X does Y", "we handle Z this way", "that flag has no effect" — must be backed by a verifiable reference *before* you state it. No citation → don't assert it.

- **External claims** (best practices, library/framework behavior, API semantics, standards) → use `WebSearch` / `WebFetch` or read the actual docs/source. Cite the URL and, for anything version-sensitive (LangChain, React, Spring, SDKs), the version you verified against. Training-data recall is not a citation — library behavior shifts between minor versions.
- **Internal claims** (how this repo does X, what a function returns, where config lives, what a migration did) → use `Read` / `Grep` / `Glob`. Cite `path:line` for the exact code you verified against, not a paraphrase.
- **Behavioral claims about running systems** (what a worker logs, what an endpoint returns, whether a test covers a path) → run the command or read the log. "It should work" is not evidence.
- **If you can't find a reference, say so explicitly** — "I couldn't verify this, treat as a guess" or "I'm recalling this from training data, please double-check." Never launder uncertainty as confidence.

Applies to research, design docs, task specs, PR descriptions, code review comments, and in-conversation recommendations. A plausible-sounding claim without a citation is a hallucination risk that costs more to unwind later than it takes to verify now.

## Boundaries

**Never:**
- Assert a fact or recommend a practice without a verifiable citation — web reference for external claims, `path:line` for internal claims (§Claims Require Evidence)
- Use bare `python3` or `uv run` for worker code — always the pinned venv at `services/worker-service/.venv/` (§Local Validation Notes)
- Point tests at the dev DB (port 55432) — tests use `par-e2e-postgres` on 55433 (§Local Validation Notes)
- Link to PRs in third-party repos from commits or PR descriptions (§External Pull Request References)
- Commit or open a PR with unverified Console UI (§Browser Verification (Console Changes))
- Merge without running the narrowest-scope tests that cover your change (§Testing (Mandatory))
- Normalize provider-shaped message content at persist-time in the worker — it breaks Anthropic prompt caching and OpenAI reasoning continuation (§LLM Provider Support)
- Reimplement LangChain's full block-translator stack in Java — the API walker is an allowlist, not a translator (§LLM Provider Support)
- Add provider-aware branches in the Console — it renders pre-normalized strings only (§LLM Provider Support)

**Ask first:**
- Force-push to `main`/`master`, destructive DB operations, shared CI/infra changes, anything that deletes data

**Always:**
- Invoke relevant superpowers skills (§Agent Skills (Superpowers))
- Use `isolation: "worktree"` when parallel subagents could touch overlapping files (§Parallel Subagent Safety)

## Parallel Subagent Safety

When orchestrating parallel subagents via the Agent tool, **always use `isolation: "worktree"`** if there is any chance two agents modify the same file — even different methods in the same file. Without worktrees, concurrent Edit tool calls on the same file can clobber each other (last writer wins, or `old_string` match fails silently).

- Before launching, check "Affected Component / File paths" for overlap — if any file appears in both scopes, use `isolation: "worktree"` on at least one agent.
- After worktree agents complete, merge their branches into the main working tree.
- Only skip worktrees when agents have truly zero file overlap (e.g., Python worker vs React console).

### Browser verification is the orchestrator's job

`make start` (and any live-stack workflow) binds global ports that aren't per-worktree. Parallel subagents racing it will collide. Split ownership for Console UI tasks:

- **Subagent:** ship code + unit tests (`make console-test`) + a new scenario in `CONSOLE_BROWSER_TESTING.md`. Never call `make start`/`make stop` or Playwright MCP tools.
- **Orchestrator:** after merge, run the Playwright scenarios once, serially. The §Browser Verification blocking gate lives here.

## LLM Provider Support

LangChain's `BaseMessage.content` is a union type (`str | List[block]`). Block shapes are provider-specific by design — they carry prompt-caching keys (Anthropic), reasoning continuation state (OpenAI Responses), and multi-modal refs that must round-trip unchanged to the LLM. **Persist content unchanged in checkpoints via `langchain_dumps`. Normalize only at read/artifact boundaries:**

- **Python paths** (compaction summarizer, `task.output.result` flattening at write-time) — delegate to `AIMessage.content_blocks` from `langchain-core ≥ 1.2`, then extract text from standard `"text"` blocks and unpack a narrow allowlist of `"non_standard"` wrappers (`output_text`, nested `message.content`, `thinking`). **Don't use `BaseMessage.text` directly** — verified against `langchain-core==1.3.0` (2026-04-17), `.text` only picks pre-normalized `{type: text, text: ...}` blocks and returns `""` for OpenAI Responses, Gemini, Bedrock, and every other provider-shaped list. Coverage of those shapes lives on `content_blocks`, and OpenAI Responses is still marked alpha (forum: *Why open ai reasoning content is not parsed into standard content blocks* — gap tracked upstream as of 1.3.0). Separator policy: the summarizer / token-count path joins sibling text blocks with `""` (programmatic concatenation — adjacent text blocks in a single AIMessage aren't paragraph boundaries for prompt-budget math); `task.output.result` passes `separator="\n\n"` so multi-block markdown (Anthropic multi-paragraph, `thinking` + prose) renders with paragraph breaks on the Console, matching the Java read-time normalizer.
- **Java API projection** (`ActivityProjectionService.extractMessageContent` / shared utility) — narrow allowlist walker covering text-bearing block shapes: `type: text`, `type: output_text`, nested `type: message → content`, `type: thinking`, and bare `{text: "..."}` dicts. When a new provider ships a novel text-bearing shape, extend the walker plus its fixture test.
- **Console** — renders the server's pre-normalized string directly. Never parses block shapes.

Adding a new LLM provider: install its `langchain-<provider>` package, verify the Python helper and the Java walker both flatten a representative message, and extend the `"non_standard"` unpack list / Java walker only for shapes not yet covered. Bedrock Converse (bare-dict) and OpenRouter (plain string) are covered by existing rules; Anthropic text, Gemini bare-dict, and OpenAI chat-completions flow through LangChain's translator stack.

## External Pull Request References

**Do not link to PRs in other people's repositories** from commits or PR descriptions — GitHub creates a cross-reference notification to the upstream author. Allowed: PRs/commits in this repo, or PRs *you* authored anywhere.

For upstream fixes in OSS dependencies, cite the released **version** (e.g., "`ddgs 9.12.1` replaced the shared executor with a per-call one"), not the PR URL. The version pin is the technical guarantee; summarize the *why* inline.

If an external PR reference slips in, rewrite the commit / PR description before merge (force-push is acceptable — process hygiene, not content change).

## Local Validation Notes

- See `README.md` and `docs/LOCAL_DEVELOPMENT.md` for setup details.
- When validating background `Makefile` targets (`make start`, `make status`, `make stop`), prefer an interactive shell / PTY.
- **Python venv:** the worker venv at `services/worker-service/.venv/` has all deps pinned. Activate with `source services/worker-service/.venv/bin/activate` or call `services/worker-service/.venv/bin/python` directly.
- **Test DB isolation:** `par-e2e-postgres` on port **55433** is the tests' DB; the dev DB on 55432 is off-limits for tests (it corrupts local state). `make worker-test` passes `E2E_DB_DSN`; `make e2e-test` passes `E2E_DB_*` vars.
- **Tracking a running task:** two surfaces answer "what's this task doing?" — the worker log (`.tmp/worker-*.log`) for runtime decisions (lifecycle, compaction, memory routing, dead-letter, retries) and `GET /v1/tasks/<id>/conversation` for what the agent actually did (turns / tool calls / tool results). `make start` runs workers at `WORKER_LOG_LEVEL=DEBUG` by default so per-turn compaction traces are already there; quiet it with `WORKER_LOG_LEVEL=INFO make start-worker`. See [Tracking a running task](./docs/LOCAL_DEVELOPMENT.md#tracking-a-running-task) for the event catalogue and jq recipes.

## Testing (Mandatory)

**Every code change must be tested before it is considered done.** No exceptions. See [LOCAL_DEVELOPMENT.md](./docs/LOCAL_DEVELOPMENT.md) for test locations, single-test commands, and conventions.

- Write tests covering the change, including failure scenarios.
- Run the **narrowest scope** that covers your change (a single test file or package is fine — you don't need to run the whole `make test` suite if it doesn't touch your change). Run `make e2e-test` after DB/schema or cross-service changes. If tests fail — including pre-existing failures — fix them before moving on.
- **CI maintenance:** when adding DB migrations, new service containers, or infra deps, verify `.github/workflows/ci.yml` picks them up. Migrations auto-apply via glob (`[0-9][0-9][0-9][0-9]_*.sql`); new services (e.g., LocalStack) must be added as CI service containers manually.

### Browser Verification (Console Changes) — BLOCKING

**Console changes are not done until verified in a real browser.** Unit tests with mocked data cannot catch cross-origin issues, encoding problems, stale data, or broken downloads. Verify with Playwright MCP tools against `make start` (Console at `localhost:5173`, API at `localhost:8080`).

For Console tasks, **read [docs/CONSOLE_TASK_CHECKLIST.md](./docs/CONSOLE_TASK_CHECKLIST.md) first** — it is the per-task merge gate. The canonical authoring rules, coverage matrix, scenario templates, and change-type → scenarios selection matrix all live in [docs/CONSOLE_BROWSER_TESTING.md](./docs/CONSOLE_BROWSER_TESTING.md). At minimum:

- Every Console change runs Scenario 1 (smoke) + every scenario the selection matrix maps to it.
- New page / dialog / form / tab → new scenario; new field on an existing form → extend that form's scenario (not just mention the section — assert field + `data-testid`).
- Changes that touch an agent-config sub-object (`memory` / `context_management` / `sandbox` / etc.) update the coverage matrix in the same commit; sub-objects rendered on >1 surface require Template D's parity assertions regardless of how many cells were cited.

---

**Status:** See [STATUS.md](./STATUS.md) for phase-level tracking and per-track progress.
