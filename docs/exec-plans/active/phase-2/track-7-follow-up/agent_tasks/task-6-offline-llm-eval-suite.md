<!-- AGENT_TASK_START: task-6-offline-llm-eval-suite.md -->

# Task 6 — Offline Real-Provider Evaluation Suite

## Agent Instructions

**CRITICAL PRE-WORK:**
1. Read GH issue [#81](https://github.com/shenjianan97/persistent-agent-runtime/issues/81) in full — motivation, non-goals, acceptance criteria.
2. Read `.github/workflows/ci.yml` — existing CI structure (service containers, migrations glob, test targets). The offline suite is a **separate workflow file**, not an addition to the existing CI.
3. Read `services/worker-service/tests/` — existing unit + integration test patterns; offline suite sits alongside `tests/` at `tests_offline/`.
4. Read the existing `cost_ledger` repo — Task 6's budget kill-switch queries cumulative spend through this interface.

**CRITICAL POST-WORK:**
1. Verify the workflow file's YAML parses (`actionlint` or `yamllint` locally).
2. Manual dispatch: run the workflow once via `gh workflow run offline-llm-eval.yml` with a real API key. Confirm at least one scenario completes and cost is logged.
3. Update `progress.md` to mark Task 6 Done.

---

## Context

All our existing tests mock the LLM. Production bugs in our conversation-shape handling, provider-specific adapter quirks, and real-provider performance characteristics are invisible to CI. PR #80's post-deploy hardening commit caught three such bugs only because production tasks failed — that's too late.

A scheduled offline suite runs a curated set of tasks against real providers on a cadence that's NOT per-commit (cost, flakiness, determinism). It catches regressions before a deploy hits a real user's task.

## Goal

A scheduled, budget-gated evaluation suite that exercises the full agent loop against real Bedrock / Anthropic / OpenAI providers on a curated set of ~5 long-running scenarios. Runs on a nightly cron + pre-release tag + manual dispatch. Never on per-commit CI. Has a hard budget kill-switch.

## Contract — Behaviour Changes

### 1. `services/worker-service/tests_offline/` scenario files

New directory sibling to `tests/`. Each scenario is a standalone pytest file marked `@pytest.mark.offline` (new pytest marker registered via `pyproject.toml` or `pytest.ini`).

Scenario set (6 scenarios; names are suggestions):

1. **`test_research_task_large_context.py`** — AWS-research-style task forcing at least one compaction firing under the Option-3 `pre_model_hook` pipeline. Asserts task completes (not dead-lettered), the replaced `summary` stays under a budget fraction of the model context window.
2. **`test_tool_use_pairing_through_compaction.py`** — task with multi-tool-call AIMessages, forces compaction at a boundary that would naively split a tool_use/tool_result pair. Verifies the `pre_model_hook`'s projection logic preserves tool_use/tool_result pairing across compaction events against a real provider. Same intent as PR #80's first-/second-firing regression coverage, updated framing for Option 3.
3. **`test_offload_recall_roundtrip.py`** (depends on Tasks 4+5 shipping) — task that triggers an offload, has the agent call `recall_tool_result`, asserts the recalled content reaches the model. **Also verifies Option C reference-replacement:** after a subsequent compaction absorbs the recalled ToolMessage into `summary`, assert (a) the corresponding entry in `state.messages` has its content replaced with a reference (not raw content), and (b) a fresh `recall_tool_result` call by the agent still returns the original content from S3.
4. **`test_memory_flush_and_dead_letter.py`** — memory-enabled agent that crosses the compaction threshold; asserts `memory_flush_fired_this_task` path runs once; also asserts `context_exceeded_irrecoverable` dead-letter path on a pathological input.
5. **`test_multi_provider_smoke.py`** — one cheap completion per provider (Bedrock, Anthropic, OpenAI) as a smoke check that credentials and adapters still work.
6. **`test_main_path_shape_through_compactions.py`** (addresses the real provider-shape regression class — the summarizer is a sub-path; PR #80's bugs were in the MAIN agent-LLM path). Task with enough tool-use turns to force the `pre_model_hook` to fire compaction at least twice. On successive turns that cross compaction boundaries, capture `llm_input_messages` returned by `pre_model_hook` and run them through `LLMConversationShapeValidator`. Validates Bedrock/Anthropic/OpenAI adapters preserve shape across first-firing, second-firing, and repeated-firing compaction events.

Each scenario:
- Uses real provider APIs — no mocks.
- Picks a **cheap** model by default (Claude Haiku 4.5, GPT-4o-mini, or similar). Scenarios MAY override via env var (`OFFLINE_LLM_EVAL_AGENT_MODEL`) but default to cheap.
- Asserts outcomes as `assert` statements with clear failure messages.
- Logs structured results (`PASS` / `FAIL` with timing + cost) to stdout for workflow annotations to ingest.

### 2. `.github/workflows/offline-llm-eval.yml` workflow

New GitHub Actions workflow. Triggers:
- `schedule` cron — nightly at a low-traffic hour, e.g. `cron: '17 8 * * *'` (UTC).
- `push` on tags matching `release-*` / `v*` (pre-release gate).
- `workflow_dispatch` (manual trigger with optional scenario filter input).

Shape:
- Single job `run-offline-eval`.
- Matrix over `provider: [bedrock, anthropic, openai]` — each provider's scenarios run in parallel. A scenario that targets one specific provider skips on the others.
- Secrets: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK` (reuse existing secret names from `model-discovery`).
- Python setup mirroring the existing worker-test job.
- Pre-run step: **budget kill-switch** (see next item).
- Run step: `.venv/bin/pytest tests_offline/ -m offline --junitxml=offline-results.xml`.
- Post-run steps:
  - Parse `offline-results.xml` → workflow annotations for each PASS / FAIL + timing.
  - Emit total-cost annotation.

### 3. Budget kill-switch (per-run hard cap + mid-run abort — v1 scope)

Cumulative-across-runs budget tracking is OUT of scope for v1 because scenarios run against an ephemeral Postgres container (§4) — no cross-run cost-ledger state persists. A "query cumulative daily spend" approach would silently no-op. V1 uses a simpler model:

- **Per-run hard cap** — `PER_RUN_OFFLINE_BUDGET_MICRODOLLARS = 1_000_000` (~$1 USD). A scenario that would exceed this MUST abort rather than continue. Enforcement is checked after each scenario completes, using the ephemeral cost-ledger total.
- **Mid-run abort** — `conftest.py` hooks into `pytest_runtest_teardown` to compute cumulative spend across already-run scenarios in the same pytest invocation. If spend exceeds the per-run cap, remaining scenarios are skipped with a `pytest.skip("per-run budget exceeded")` message and workflow annotation. Already-run scenarios' results still report.
- **Matrix-job provider parallelism** — each provider in the `matrix` has its own per-run cap; they don't share budget state. Accept this as a v1 limitation: worst case is `3 × PER_RUN_CAP = $3` per run × nightly ≈ $90/month. Well within a reasonable offline-evaluation budget.
- **v2 (tracked as a follow-up, not in this task):** shared cross-run budget state via a dedicated table on the non-ephemeral dev/staging DB, or a Redis bucket. Gate promotion on whether v1's $90/month worst case becomes uncomfortable.

### 4. Test isolation

- Scenarios run against an **ephemeral database** (fresh PG container in the workflow, same approach as `make e2e-test`) — do NOT write to any shared DB.
- Scenarios use a reserved `tenant_id='offline-llm-eval'` so cost-ledger queries can exclude their spend from customer billing views.

### 5. Per-commit CI is unchanged

Explicitly verify: existing `.github/workflows/ci.yml` does NOT invoke the offline suite. Offline tests are excluded by default pytest config (either by marker exclusion or by the `tests/` vs `tests_offline/` directory split). Per-commit CI stays fast, hermetic, deterministic.

## Affected Files

- `services/worker-service/tests_offline/` — **new directory** with 5 scenario files and a shared `conftest.py` (for the `offline` marker and budget-guard fixture).
- `services/worker-service/tests_offline/README.md` — runbook: how to run locally, what happens on failure, where to find result annotations.
- `services/worker-service/pyproject.toml` — register `offline` marker (in `tool.pytest.ini_options.markers`) and exclude `tests_offline` from default collection.
- `.github/workflows/offline-llm-eval.yml` — **new workflow**.
- `services/worker-service/tests_offline/_budget_guard.py` — kill-switch script.

## Dependencies

None at code level. Benefits from Tasks 1–5 being in place when the suite runs, but scenarios gracefully skip (not fail) when a dependency isn't yet deployed (e.g. `test_offload_recall_roundtrip.py` asserts the feature flag exists and skips if not).

## Out of Scope for This Task

- Integrating with a managed eval platform (LangSmith, promptfoo). Pure pytest + GH Actions is sufficient for v1.
- Custom eval metrics beyond pass/fail + timing + cost (semantic similarity, LLM-as-judge). Future extension if we need subjective quality signals.
- Regression storage — we don't persist historical results in v1. Workflow annotations and the Actions run history are the ops view.
- Running against production data. Scenarios use synthetic, check-in-the-repo fixtures. Zero PII.

## Acceptance Criteria (observable behaviours)

1. `services/worker-service/tests_offline/` exists with 6 scenario files + shared `conftest.py`. `grep -r "@pytest.mark.offline" tests_offline/` lists at least one mark per scenario.
2. `services/worker-service/.venv/bin/pytest tests/` (per-commit path) does NOT collect anything from `tests_offline/`.
3. `services/worker-service/.venv/bin/pytest tests_offline/ -m offline` collects and runs the scenarios.
4. `.github/workflows/offline-llm-eval.yml` has `schedule`, `push: tags`, and `workflow_dispatch` triggers. `actionlint` passes.
5. Per-run budget: a fake fixture forcing accumulated cost to exceed `PER_RUN_OFFLINE_BUDGET_MICRODOLLARS` mid-suite causes remaining scenarios to be skipped (not failed) with a "per-run budget exceeded" skip message. Already-run scenarios' results still report.
6. Scenario 6 (`test_main_path_shape_through_compactions.py`) captures `llm_input_messages` at first, second, and third compaction firings (via `pre_model_hook`) and asserts shape-validator cleanliness for each.
7. Scenario 3 (`test_offload_recall_roundtrip.py`) asserts the Option C reference-replacement behaviour: after a compaction that absorbs a recalled ToolMessage, `state.messages` stores a reference (not raw content) for that message AND a fresh `recall_tool_result` call still returns the original content.
7. Manual dispatch (`gh workflow run offline-llm-eval.yml -f scenario=test_multi_provider_smoke`) completes end-to-end with real providers and produces workflow annotations for each scenario's outcome. **This is a manual verification step — not automatable in this task.**
8. Runbook `tests_offline/README.md` covers: local invocation, failure triage steps, how to add a new scenario, cost expectations, the v1 per-run-only budget limitation and when to escalate to the v2 shared-state approach.

## Pattern references in existing code

- Test marker registration: `services/worker-service/pyproject.toml:tool.pytest.ini_options` (existing filterwarnings + asyncio_mode).
- Service container setup in CI: `.github/workflows/ci.yml` — the e2e job's Postgres service is the model.
- Cost-ledger query: `services/worker-service/core/cost_ledger_repository.py` — aggregates by `tenant_id` / time window. Budget guard uses the same interface.

<!-- AGENT_TASK_END -->
