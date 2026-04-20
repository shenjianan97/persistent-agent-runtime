<!-- AGENT_TASK_START: task-8-system-prompt-chunking.md -->

# Task 8 — Default System Prompt: Chunked-Artifact Guidance

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — root cause for the worst-case generation: model packs a 7k-token report into one `create_text_artifact` tool argument.
3. The default system prompt assembly in the worker (locate via `grep -rn "create_text_artifact" services/worker-service/`). The exact file is likely `services/worker-service/executor/system_prompts.py` or assembled inside `executor/graph.py` near the agent_node setup. Find the canonical site before editing.
4. `services/worker-service/executor/tools/...` for the `create_text_artifact` tool definition — confirm whether the tool supports incremental writes (append mode, multiple calls with the same artifact name, etc.) or whether the prompt guidance must direct the agent to split logically across separately-named artifacts.

**CRITICAL POST-WORK:** After completing this task:
1. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_system_prompts.py -v` (locate or create this test file).
2. Update `progress.md` row 8 to "Done".

## Context

The pathological case from #85 — a 7k-token tool-use block emitted in a single shot — is the dominant cause of the timeout-class failure. Even with streaming + maxTokens caps, the agent producing such a giant tool argument is the root behavioral pattern. Steering the model to write incrementally is the single highest-leverage prompt-side fix.

The guidance is **conditional** on `create_text_artifact` (or whatever the artifact tool's registered name is) being present in the agent's allowed tools. Agents without artifact tools should not see irrelevant guidance.

## Task-Specific Shared Contract

- Guidance is appended to (not replacing) the existing system prompt assembled per-agent.
- Guidance is gated on the artifact tool being in `agent_config.allowed_tools` (or however the runtime detects available tools — locate the actual check).
- Wording is **direct and concrete**: tells the agent that long deliverables (>1500 words / >2000 tokens) should be split into multiple `create_text_artifact` calls, with section headers indicating the role of each artifact ("part 1: introduction", "part 2: features A-D", etc.). It is not a soft suggestion; treat it as a directive.
- A short rationale is included so the agent understands the constraint isn't arbitrary: "Generating a single very large tool argument can exceed inference timeouts. Split long outputs into multiple artifacts."
- Wording is reviewed by the orchestrator before merge — propose 2 wording variants in the PR description and let the reviewer pick.

## Affected Component

- **Service/Module:** Worker — Executor (system prompt assembly)
- **File paths:**
  - `services/worker-service/executor/system_prompts.py` (modify — or wherever the per-agent system prompt is assembled)
  - `services/worker-service/tests/test_system_prompts.py` (new or extend)
- **Change type:** modification + new tests

## Dependencies

- **Must complete first:** None.
- **Provides output to:** Task 9 (acceptance criteria asserts the guidance is present in the prompt for the repro task's agent).
- **Shared interfaces/contracts:** none — the change is internal to the system-prompt assembly path.

## Implementation Specification

### Locate the assembly site

Use the codebase's existing system-prompt builder (likely `assemble_system_prompt(agent_config) -> str` or similar). If no single function exists, refactor lightly to introduce one — keep the diff small and focused on this concern.

### Add the conditional guidance

When the agent's allowed tools contain the artifact tool's name, append a new paragraph (kept under 6 sentences, ~120 words) covering:

1. The threshold for chunking (≥ 1500 words / ≥ 2000 tokens of intended output).
2. The instruction: emit multiple `create_text_artifact` calls with descriptive section names.
3. Short rationale (one sentence).
4. Optional pattern hint: "If the deliverable has natural sections, name artifacts by section. Otherwise number them: report-part-1, report-part-2."

### Tests

In `tests/test_system_prompts.py`:

- Agent config with `allowed_tools` containing `create_text_artifact`: assembled prompt contains a substring matching the chunking guidance (assert against a stable phrase, not the full text — wording may be tuned).
- Agent config without `create_text_artifact`: assembled prompt does **not** contain the chunking guidance.
- Agent config with `create_text_artifact` plus other tools: chunking guidance appears exactly once.
- The chunking guidance does not duplicate any existing prompt text (avoid running through repeats on prompt re-assembly).

## Acceptance Criteria

- [ ] System prompts for agents with the artifact tool include explicit chunking guidance.
- [ ] System prompts for agents without the artifact tool are unchanged.
- [ ] Tests above pass.
- [ ] Two wording variants proposed in the PR description for orchestrator review.

## Out of Scope

- Modifying the `create_text_artifact` tool itself to enforce a per-call size limit.
- Adding "chunked artifact" affordance to other tools.
- A/B testing the prompt against the pre-change baseline (would be valuable but is a follow-up project).

<!-- AGENT_TASK_END -->
