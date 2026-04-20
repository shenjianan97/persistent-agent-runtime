<!-- AGENT_TASK_START: task-7-context-window-coverage.md -->

# Task 7 — Complete `CONTEXT_WINDOW_DEFAULTS` Coverage

## Agent Instructions

**CRITICAL PRE-WORK:**
1. Read `services/model-discovery/main.py` — specifically `CONTEXT_WINDOW_DEFAULTS`, `CONTEXT_WINDOW_FALLBACKS`, `DEACTIVATE_MODEL_IDS`, and `resolve_model_context_window`. PR #80 introduced these.
2. Read `tests/test_discover_models.py` — specifically `test_platform_floor_consistent_with_deny_list`. This invariant MUST continue to hold after your changes.
3. Understand the current coverage: run `docker exec <pg-container> psql ... -c "SELECT model_id FROM models WHERE is_active = true AND context_window IS NULL"` against a local dev DB to list currently-unpopulated active models. (If no dev DB is running, `grep "\"" services/model-discovery/main.py` around `CONTEXT_WINDOW_DEFAULTS` shows what IS covered; everything else in the active-models list falls to provider fallback.)

**CRITICAL POST-WORK:**
1. Run `cd services/worker-service && .venv/bin/python -m pytest ../../tests/test_discover_models.py -v`. All tests green, especially `test_platform_floor_consistent_with_deny_list`.
2. Run `services/model-discovery/main.py` against a dev DB with all provider keys present; spot-check that the newly-covered models have populated `context_window` values.
3. Update `progress.md` to mark Task 7 Done.

---

## Context

PR #80's hardening commit populated explicit `CONTEXT_WINDOW_DEFAULTS` entries for the three model families we were actively running (Z.AI GLM, Anthropic Claude 4.5–4.7, OpenAI GPT-4.1/4o/5/o-series, Amazon Nova). The remaining ~60% of active models (Gemini, Mistral, Nvidia, Qwen, Moonshot, Writer) fall to the provider-level 128K fallback. Provider fallback is safe because `DEACTIVATE_MODEL_IDS` filters legacy sub-128K entries, but it's a fallback — explicit verified values are better for the following reasons:

- Larger models (Qwen3-VL-235B at 256K, Mistral Large-3 at 128K, Nemotron-super at 300K) have their real budgets exposed to the compaction pipeline.
- Fallback WARN noise in observability stays low — only genuinely unknown models trigger it.
- The `test_platform_floor_consistent_with_deny_list` invariant gets stronger as more models are explicitly declared.

## Goal

Every active model family represented in the `models` table has a verified explicit `CONTEXT_WINDOW_DEFAULTS` entry. Provider fallback becomes the exception, not the rule.

## Contract — Behaviour Changes

### 1. Extended `CONTEXT_WINDOW_DEFAULTS`

- Add entries for the following families. **Each value MUST be verified against the provider's official documentation or the AWS Bedrock model card at the time of implementation.** Do NOT guess. A wrong value overshoots the model's real ceiling and produces runtime errors.

**Before adding any specific model_id below**, confirm it actually exists in the currently-discovered `models` table via:

```
docker exec <pg-container> psql -U postgres -d persistent_agent_runtime \
  -c "SELECT model_id FROM models WHERE is_active = true ORDER BY model_id;"
```

The list below is a starting reference as of 2026-04 — some entries may have been renamed, deprecated, or never actually shipped to the Bedrock Converse API. **Drop any model_id that doesn't appear in your local discovered table.** Add any active model_id you find that isn't listed here to the appropriate family.

Model families to cover (each entry is `"model_id": <int tokens>`):

- **Google Gemma** on Bedrock: `google.gemma-3-4b-it`, `google.gemma-3-12b-it`, `google.gemma-3-27b-it`.
- **Mistral** on Bedrock: `mistral.mistral-large-3-675b-instruct`, `mistral.devstral-2-123b`, `mistral.magistral-small-2509`, `mistral.ministral-3-3b-instruct`, `mistral.ministral-3-8b-instruct`, `mistral.ministral-3-14b-instruct`, `mistral.voxtral-mini-3b-2507`, `mistral.voxtral-small-24b-2507`.
- **Nvidia Nemotron** on Bedrock: `nvidia.nemotron-nano-3-30b`, `nvidia.nemotron-nano-9b-v2`, `nvidia.nemotron-nano-12b-v2`, `nvidia.nemotron-super-3-120b`.
- **Alibaba Qwen** on Bedrock: `qwen.qwen3-32b-v1:0`, `qwen.qwen3-coder-30b-a3b-v1:0`, `qwen.qwen3-coder-next`, `qwen.qwen3-next-80b-a3b`, `qwen.qwen3-vl-235b-a22b`.
- **Moonshot Kimi** on Bedrock: `moonshot.kimi-k2-thinking`, `moonshotai.kimi-k2.5`.
- **Writer Palmyra** on Bedrock: `writer.palmyra-x4-v1:0`, `writer.palmyra-x5-v1:0`, `writer.palmyra-vision-7b`.
- **OpenAI gpt-oss variants** on Bedrock (if present in the active list): `openai.gpt-oss-20b-1:0`, `openai.gpt-oss-120b-1:0`, `openai.gpt-oss-safeguard-20b`, `openai.gpt-oss-safeguard-120b`.
- **MiniMax** on Bedrock: `minimax.minimax-m2`, `minimax.minimax-m2.1`, `minimax.minimax-m2.5`.
- **Amazon Nova 2** on Bedrock: `amazon.nova-2-lite-v1:0` (Nova Pro/Lite/Micro v1 already covered in PR #80).
- **Anthropic direct API**: any claude-haiku-4-5 / claude-sonnet-4-5 / claude-opus-4-5 / claude-opus-4-7 aliases listed in `claude-haiku-4-5-20251001` style that aren't already covered.
- **OpenAI**: verify `gpt-5.1`, `gpt-5.2`, `gpt-5.3`, `gpt-5.4`, `gpt-audio*`, `gpt-realtime*`, `o1-pro`, `gpt-image*` families. Audio / image / realtime families MAY not have traditional "context windows" — if so, omit them from `CONTEXT_WINDOW_DEFAULTS` (they'll fall to provider fallback with a WARN; audio/image paths don't hit the compaction pipeline anyway).

### 2. Verify the platform-floor invariant still holds

- After adding entries, run `pytest tests/test_discover_models.py::test_platform_floor_consistent_with_deny_list`.
- If any newly-added entry has `context_window < GLOBAL_FALLBACK_CONTEXT_WINDOW (128_000)`, the model MUST be added to `DEACTIVATE_MODEL_IDS` as well. (In practice this shouldn't happen — all modern chat models are ≥128K; if you hit a sub-128K model, add it to the deny list.)

### 3. Idempotent re-discovery

- Add a test (or extend an existing one) that exercises a discovery run against an in-memory fake DB and asserts the context_window field is populated for a sampling of newly-covered models.

## Affected Files

- `services/model-discovery/main.py` — extended `CONTEXT_WINDOW_DEFAULTS` entries.
- `tests/test_discover_models.py` — extended test coverage for new entries; invariant test continues to pass.

## Dependencies

None. Task 7 is independent and trivially parallelisable with all other tasks.

## Out of Scope for This Task

- Auto-discovery of context windows from provider APIs. Bedrock's `describe-foundation-model` returns context info inconsistently; Anthropic / OpenAI don't expose it programmatically. Manual verification is the only reliable path for v1. A future task could explore programmatic discovery for providers that support it.
- Changing `CONTEXT_WINDOW_FALLBACKS` or `GLOBAL_FALLBACK_CONTEXT_WINDOW` — those stay as PR #80 set them.
- Changing `DEACTIVATE_MODEL_IDS` semantics. Add entries only if a newly-covered model is sub-128K.
- Any interaction with the Option 3 `pre_model_hook` + replace-and-rehydrate pipeline changes. This task is purely about data population in the `models` table; it does not touch runtime compaction behaviour.

## Acceptance Criteria (observable behaviours)

1. After the change, re-running discovery against a local dev DB with all provider keys present results in **zero active models** having `context_window IS NULL` — except for `text-embedding-3-small` (not applicable) and any audio/image/realtime OpenAI models you intentionally omitted.
2. For every newly-added `CONTEXT_WINDOW_DEFAULTS` entry, a source citation appears in a code comment adjacent to the entry (URL to the provider's model card / docs).
3. `test_platform_floor_consistent_with_deny_list` passes.
4. Existing `test_resolve_model_context_window_returns_explicit_value` test extends to assert at least 5 of the newly-covered model IDs return their expected explicit value.
5. No change to the `WARN` fallback log frequency for covered model families — i.e. a local discovery + local task using a newly-covered model does NOT emit `compaction.model_context_window_unknown`.

## Pattern references in existing code

- Existing `CONTEXT_WINDOW_DEFAULTS` entries (added in PR #80) — follow the same comment style with a model-card URL on the line above each provider group.
- `test_platform_floor_consistent_with_deny_list` in `tests/test_discover_models.py` — already enforces the invariant; no new test infrastructure needed.
- Provider-specific naming conventions: the Bedrock `foundation-models` API exposes `modelId` strings (e.g. `zai.glm-5`) — match exactly.

<!-- AGENT_TASK_END -->
