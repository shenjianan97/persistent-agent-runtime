# Offline Real-Provider Evaluation Suite

This directory holds the **scheduled** real-provider evaluation scenarios for
the worker service. It is deliberately excluded from per-commit CI
(`.github/workflows/ci.yml`) and from `make test` / `make worker-test` — the
scenarios make live LLM calls, cost real money, and are flakier than unit
tests.

Track 7 Follow-up: [Task 6 spec](../../../docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-6-offline-llm-eval-suite.md).

---

## TL;DR

- **Per-commit CI never runs these.** `pyproject.toml` pins `testpaths =
  ["tests"]` and adds `tests_offline` to `norecursedirs`.
- **The scheduled workflow (`.github/workflows/offline-llm-eval.yml`) runs
  them.** Triggers: nightly cron, `release-*` / `v*` tags, and
  `workflow_dispatch`.
- **Per-run budget cap is ~$1.** Exceeding it skips remaining scenarios in the
  same pytest invocation cleanly (not fail). See **Budget** below.

---

## Scenarios (6)

| # | File | Purpose |
|---|------|---------|
| 1 | `test_research_task_large_context.py` | AWS-research-style task forcing ≥1 compaction firing; asserts task completes + summary stays under budget fraction. |
| 2 | `test_tool_use_pairing_through_compaction.py` | Multi-tool-call turns; asserts `pre_model_hook` projection preserves tool_use/tool_result pairing through compaction. Real-provider replay of PR #80's unit coverage. |
| 3 | `test_offload_recall_roundtrip.py` | Ingestion offload + `recall_tool_result` round-trip; asserts Option-C reference-replacement preserves recallability after summarization. **Depends on Tasks 4+5.** |
| 4 | `test_memory_flush_and_dead_letter.py` | Memory-enabled agent: asserts `memory_flush_fired_this_task` runs once; asserts pathological input dead-letters with `context_exceeded_irrecoverable`. |
| 5 | `test_multi_provider_smoke.py` | One cheap completion per provider (Bedrock / Anthropic / OpenAI) — credentials + adapter smoke check. |
| 6 | `test_main_path_shape_through_compactions.py` | Captures `llm_input_messages` at first / second / third `pre_model_hook` firings; runs each through `LLMConversationShapeValidator`; asserts cleanliness. MAIN-path shape regression coverage. |

Scenarios mark themselves `@pytest.mark.offline`. The `conftest.py` also
auto-applies the marker at collection time as a safety net.

---

## Local invocation

You need either a full worker venv (`services/worker-service/.venv/`) or any
Python 3.11 env with the `worker-service[dev]` extras installed. Collection
should work without any secrets or Postgres — individual scenarios skip
themselves based on missing prerequisites.

```bash
# From repo root.

# Collect only — sanity-check the suite imports without touching providers.
services/worker-service/.venv/bin/pytest \
  services/worker-service/tests_offline/ --collect-only -q

# Run one scenario against real Anthropic (requires ANTHROPIC_API_KEY).
# This costs money. Scenarios auto-skip if their env vars aren't set.
services/worker-service/.venv/bin/pytest \
  services/worker-service/tests_offline/test_multi_provider_smoke.py \
  -m offline -ra -q

# Full suite, as the workflow runs it.
services/worker-service/.venv/bin/pytest \
  services/worker-service/tests_offline/ \
  -m offline \
  --junitxml=/tmp/offline-results.xml \
  -ra -q
```

### Env vars scenarios expect

| Var | Purpose | Default |
|-----|---------|---------|
| `OFFLINE_LLM_EVAL_PROVIDER` | Matrix cell identifier (`bedrock` / `anthropic` / `openai`) | `anthropic` (local) |
| `OFFLINE_LLM_EVAL_AGENT_MODEL` | Cheap default model ID | `claude-haiku-4-5` |
| `OFFLINE_LLM_EVAL_TENANT_ID` | Reserved tenant for cost-ledger exclusion | `offline-llm-eval` |
| `OFFLINE_LLM_EVAL_BUDGET_MICRODOLLARS` | Override the ~$1 per-run cap | `1_000_000` (~$1) |
| `ANTHROPIC_API_KEY` | Anthropic API key | — (scenario skips without it) |
| `OPENAI_API_KEY` | OpenAI API key | — (scenario skips without it) |
| `AWS_BEARER_TOKEN_BEDROCK` | Bedrock bearer token (same name used by `model-discovery`) | — (scenario skips without it) |
| `E2E_DB_DSN` / `E2E_DB_*` | Ephemeral Postgres DSN — scenarios that need a DB read this | — (scenario skips without it) |

---

## Budget — how the kill-switch works (v1)

- Hard cap: `PER_RUN_OFFLINE_BUDGET_MICRODOLLARS = 1_000_000` (~$1 USD).
  Declared in `_budget_guard.py`; overridable via
  `OFFLINE_LLM_EVAL_BUDGET_MICRODOLLARS`.
- **Scope: per pytest invocation.** No cross-run / daily state.
- **How it triggers:**
  1. Each scenario records its observed microdollar spend via the
     `record_spend` fixture at the end of the test body.
  2. `conftest.py::pytest_runtest_teardown` inspects the running total.
  3. `conftest.py::pytest_runtest_setup` (tryfirst) checks the cap BEFORE
     the next scenario's setup runs; if over cap, calls
     `pytest.skip("per-run budget exceeded")`.
- **Matrix-job isolation:** each provider in the workflow matrix gets its own
  process-local accumulator. Worst case nightly cost is
  `3 × cap = ~$3 × 30 = ~$90 / month`.

### v1 limitation and v2 escalation

**v1 is per-run-only.** Cross-run or daily cumulative state is deferred. That
means:

- A hung / flaky workflow that retries many times within a day could exceed
  the $90/month worst case.
- Different matrix cells can't starve each other out — by design.

**Escalate to v2 when:**

- Monthly AWS/Anthropic/OpenAI bills show nightly offline spend >$5/night
  sustained for a week.
- Someone introduces a per-commit invocation of the suite by accident (treat
  that as an infrastructure bug first; fix pyproject / workflow selection).

**v2 design sketch** (see plan.md §A5): shared cross-run budget state via
either a dedicated table on the non-ephemeral dev/staging DB, or a Redis
bucket. Gate the switch on whether v1's worst case actually hurts.

---

## Triage on failure

Scheduled run failed? Follow this order.

1. **Open the Actions run.** `gh run list --workflow=offline-llm-eval.yml
   --limit 5` then `gh run view <run-id> --log`.
2. **Look at the `offline-results.xml` artifact** first. JUnit XML gives you
   PASS/FAIL per scenario + the failure message.
3. **Check the budget summary section.** The terminal reporter prints
   `OFFLINE_BUDGET_SPENT_MICRODOLLARS=...` and a `skipped` list at the end of
   the pytest output. If scenarios skipped with "per-run budget exceeded", the
   cap tripped — either the scenarios grew expensive or the `record_spend`
   recording overshot. Investigate the ephemeral-ledger totals first, then
   the cap.
4. **Scenarios 1/2/3/4/6 depend on Tasks 2-5** of the follow-up. If those
   haven't shipped on the branch the suite is pointed at, the scenarios will
   cleanly skip with `importorskip` messages; that's not a failure — the
   run is a no-op until the pipeline lands.
5. **Scenario 5 (multi-provider smoke) is the credential canary.** If it's
   the only thing failing, 9/10 times an API key rotated or a model slug
   retired upstream.

If a real-provider scenario FAILS (not skipped) — open an issue and include:

- JUnit failure message + tail of the pytest log
- The scenario's `record_spend` total if available
- The provider (matrix cell) and model ID used

---

## Adding a new scenario

1. Create `tests_offline/test_<descriptive_name>.py`.
2. Decorate with `@pytest.mark.offline` (the auto-marker also does this, but
   decorating explicitly aids `grep` and `-m` filtering from other dirs).
3. Gate new-code dependencies via `pytest.importorskip("...")` at module top
   so the scenario skips cleanly when run against a branch that hasn't
   landed its prerequisites.
4. At scenario body end, call `record_spend(cost_microdollars)` with the
   cost your scenario observed. Skip scenarios may pass `0`.
5. For scenarios that target a specific provider, compare against the
   `offline_provider` fixture and `pytest.skip(...)` on mismatch — the
   matrix runs each cell independently.
6. Update this README's scenario table.

**Do NOT** add scenarios that:

- Depend on production data or specific tenant rows (use synthetic fixtures).
- Assume cross-run state (no cumulative-spend assertions — v1 resets per
  invocation).
- Require Console / API service to be running locally.

---

## Cost expectations

With the cheap-by-default model choices and the ~$1 per-run cap:

- Smoke (Scenario 5): ~$0.001 per completion × 3 = ~$0.003 per run.
- Compaction scenarios (1, 2, 4, 6): each expected ~$0.05 - $0.20 per run.
  Exact cost depends on how many tool-use turns the scenario drives; the cap
  is the ceiling.
- Recall scenario (3): ~$0.05 - $0.15 per run depending on how many S3
  round-trips the agent makes.

Nightly cron × 30 days × 3 providers ≈ $90/month worst case; realistic
spend target is ~$15/month.

---

## References

- Task spec: `docs/exec-plans/active/phase-2/track-7-follow-up/agent_tasks/task-6-offline-llm-eval-suite.md`
- Track plan: `docs/exec-plans/active/phase-2/track-7-follow-up/plan.md`
- Issue: [#81](https://github.com/shenjianan97/persistent-agent-runtime/issues/81)
- Workflow: `.github/workflows/offline-llm-eval.yml`
- Parent Track 7 design: `docs/design-docs/phase-2/track-7-context-window-management.md`
