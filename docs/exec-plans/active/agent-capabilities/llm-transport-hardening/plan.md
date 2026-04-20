# LLM Transport Hardening — Orchestrator Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Per project convention (CLAUDE.md → Task spec detail level), agent_tasks contain **contracts**, not paste-ready code.

**Tracking issue:** [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — Worker LLM calls: no streaming, no enforced timeout, no maxTokens cap.

**Goal:** Convert the worker's LLM call site from a silent blocking `ainvoke` with a non-functional 300 s timeout into a streamed call with a real per-chunk inactivity timeout, a hard `maxTokens` ceiling, live progress events surfaced into the conversation log, and a system-prompt nudge that prevents the agent from packing multi-thousand-token reports into a single tool argument.

**Architecture:**
1. **`executor/providers.py`** stops using `init_chat_model(...)` for **all three providers** (Bedrock, OpenAI, Anthropic) and constructs each provider's chat-model class directly. Each provider's native timeout / retries / max-tokens fields are used (Bedrock: `botocore.Config(read_timeout, connect_timeout)` on a pre-built boto3 client; OpenAI: `request_timeout=`, `max_retries=`, `max_tokens=` on `ChatOpenAI`; Anthropic: `default_request_timeout=`, `max_retries=`, `max_tokens=` on `ChatAnthropic`). All three values come from a single `LLMTransportConfig` resolver that consults agent config first, then platform defaults.
2. **`executor/graph.py`** replaces `llm_with_tools.ainvoke(messages_for_llm, config)` at line 1173 with `llm_with_tools.astream(...)`. Chunks are accumulated into a single `AIMessageChunk` (LangChain's chunk-merge protocol via `+`), then materialized into the same `AIMessage` shape the rest of the graph expects. Non-streaming behavior of downstream code (cost ledger, conversation log append, `Command` returns) does not change.
3. **`core/conversation_log_repository.py`** gains a new `ConversationLogKind` for `llm_stream_progress` carrying `{chunks, chars, tool_call_chars, elapsed_s}`. The streaming loop in `agent_node` emits one progress entry every N seconds (default 10 s, configurable) and one terminal `llm_stream_complete` entry. Idempotency keys are deterministic per checkpoint + sequence so retries don't double-count.
4. **`services/console/`** renders these progress entries on the task page (live tail). Existing `useTaskEvents` / SSE paths are unchanged; only the new entry kind needs a render branch.
5. **Default system prompt** (where `create_text_artifact` is described to the agent) is updated to direct the agent to chunk long deliverables across multiple incremental tool calls rather than packing the whole document into one tool argument.

**Tech Stack:** Python (worker, langchain-aws, botocore, asyncpg) · TypeScript / React 19 (Console) · PostgreSQL — **includes one schema migration** to extend `task_conversation_log.chk_task_conversation_log_kind` (defined at `infrastructure/database/migrations/0017_task_conversation_log.sql:60-71`) with the two new kinds. Both Python (`_VALID_KINDS` in `core/conversation_log_repository.py`) and the DB CHECK constraint must list the same allowlist or inserts violate the constraint and progress events silently fail.

---

## A1. Implementation Overview

The five fixes track the five layers identified in #85's root cause analysis:

1. **Real client-level timeout config — all providers.** `init_chat_model` silently moves *unknown* kwargs into `model_kwargs`. Bedrock's failure mode was the loudest (langchain's startup warning proved `timeout=` and `max_retries=` were dropped) but **the same hazard exists for OpenAI and Anthropic**: their native field names are `request_timeout` and `default_request_timeout` respectively, not `timeout`. Anyone passing the wrong name silently gets default behavior. Direct per-provider construction with each provider's native field names guarantees the timeout we configure is the one that actually applies.
2. **`maxTokens` safety belt.** Without `inferenceConfig.maxTokens` Bedrock generates until the model says `end_turn`. Slow models (e.g., `zai.glm-5` at ~35 tok/s) can run for minutes. Setting a per-agent cap (default 16 384) with a structured warning when `stopReason=max_tokens` keeps a misbehaving model bounded.
3. **Streaming.** `ainvoke` blocks until the entire response is buffered; the worker has no mid-call signal. `astream` resets the per-read timeout each chunk, so a 4-min generation succeeds; partial state is observable; cancellation becomes meaningful.
4. **Per-task progress events.** Streaming alone is invisible to operators unless we surface the chunks. A small repeating `llm_stream_progress` entry on the conversation log lets the Console show "agent generating, 12 s, 1834 chars" instead of a frozen page.
5. **Prompt guidance for chunked artifacts.** The pathological case from #85 was the model packing a 7k-token report into a single `create_text_artifact` tool argument. Updating the default system prompt to instruct chunked, incremental writes addresses the worst-case generation length at its source — independent of model speed.

**Canonical references the implementing agent must read** before touching code:

- This plan and `progress.md`.
- Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — the symptom, evidence, and acceptance criteria.
- `services/worker-service/executor/providers.py` — current LLM construction site (entire file, ~45 lines).
- `services/worker-service/executor/graph.py` lines 1060–1210 — the `agent_node` body where the LLM is invoked, where compaction runs before each call, where the conversation log append happens, and where rate-limit retries live.
- `services/worker-service/core/conversation_log_repository.py` — `append_entry` signature, `ConversationLogKind` literal, idempotency-key contract.
- LangChain `AIMessageChunk` documentation — chunks merge via `+`; tool calls accumulate via `tool_call_chunks` field (not `tool_calls`); `response_metadata` and `usage_metadata` arrive only on the final chunk.
- `langchain-aws` `ChatBedrockConverse` source — for the `client`, `config`, and `max_tokens` field semantics. Confirmed available via `model_fields` inspection: `config: Any = None`, `client: Any = None`, `max_tokens: Optional[int] = None`.

---

## A2. Impacted Components

| Component | Path | Change Type | Description |
|-----------|------|-------------|-------------|
| LLM transport config | `services/worker-service/executor/providers.py` | rewrite | Drop `init_chat_model(...)` for **all three providers** and construct each provider's chat-model class directly with provider-native transport fields. Bedrock: `ChatBedrockConverse(client=boto3.client("bedrock-runtime", config=botocore.Config(read_timeout, connect_timeout, retries={"max_attempts": 0})), max_tokens=...)`. OpenAI: `ChatOpenAI(request_timeout=read_timeout_s, max_retries=0, max_tokens=...)`. Anthropic: `ChatAnthropic(default_request_timeout=read_timeout_s, max_retries=0, max_tokens=...)`. All three consume the same `LLMTransportConfig` from `transport.py`. |
| Transport defaults + resolver | `services/worker-service/executor/transport.py` (new) | new | `LLMTransportConfig` dataclass + `resolve_transport(agent_config, model)` returning `(connect_timeout, read_timeout, max_tokens)`. Platform defaults: `connect=10`, `read=120`, `max_tokens=16_384`. Per-agent overrides via `agent_config.llm_transport.{connect_timeout_s, read_timeout_s, max_output_tokens}` (all optional). |
| Agent config (API) | `services/api-service/.../request/LlmTransportConfigRequest.java` (new) + `AgentConfigRequest.java` (modify) + `ConfigValidationHelper.java` (modify) + `AgentService.java` (modify) | new + modification | Add nested `llm_transport` sub-object with the three optional override fields; canonicalisation round-trip; validation: `connect_timeout_s ∈ [1, 60]`, `read_timeout_s ∈ [10, 900]`, `max_output_tokens ∈ [256, 200_000]`. |
| LLM call streaming | `services/worker-service/executor/graph.py:1170-1210` | rewrite | Replace `ainvoke` with `astream`; accumulate chunks; emit progress events via the conversation log every N seconds. Preserve current rate-limit retry loop, cost ledger attribution, conversation-log final append, and `Command` return shape. |
| Conversation log kind | `services/worker-service/core/conversation_log_repository.py` + `infrastructure/database/migrations/0018_conversation_log_streaming_kinds.sql` (new) | modification + new migration | Add two literals to `ConversationLogKind`: `llm_stream_progress` and `llm_stream_complete`. Update Python `_VALID_KINDS`. **Migration drops + re-adds `chk_task_conversation_log_kind` with the extended allowlist** (DROP CONSTRAINT + ADD CONSTRAINT pattern; Postgres CHECK constraints are not ALTER-able in place). Verify CI's migration glob (`[0-9][0-9][0-9][0-9]_*.sql`) auto-picks the new file. |
| Conversation log writer helpers | `services/worker-service/executor/graph.py` (`_convlog_append_*` helpers) | modification | Add `_convlog_append_stream_progress(...)` and `_convlog_append_stream_complete(...)` mirroring the existing `_convlog_append_llm_response` pattern. Idempotency-key format: `f"{checkpoint_id}:stream:{seq}"` so a re-played super-step does not double-write. |
| Default system prompt | `services/worker-service/executor/system_prompts.py` (or equivalent — locate via grep) | modification | Add chunked-artifact guidance whenever `create_text_artifact` (or its registered name) is in the agent's allowed tools. Guidance text must steer the model away from one-shot multi-thousand-token tool-use blocks. Wording reviewed by orchestrator before merge. |
| Console live tail | `services/console/src/features/tasks/...` (locate via grep `useTaskEvents` or `conversation_log` rendering) | modification | New render branch for `llm_stream_progress` (rolling line) and `llm_stream_complete` (collapses into the prior LLM response card). No new SSE plumbing. |
| Tests — worker unit | `services/worker-service/tests/test_providers_transport.py` (new), `tests/test_graph_streaming.py` (new) | new | Provider builds a client with the configured `read_timeout`; `astream` integration produces the same final `AIMessage` as `ainvoke` for a recorded fixture; progress events fire at the configured cadence; `stopReason=max_tokens` surfaces a structured warning. |
| Tests — API integration | `services/api-service/.../AgentConfigValidationTest.java`, `AgentServiceCanonicalizeTest.java` | extend | `llm_transport` validation bounds; canonicalisation round-trip; absent sub-object is omitted from persisted JSON. |
| Tests — Console browser | `docs/CONSOLE_BROWSER_TESTING.md` + scenario file | extend | New scenario: long-running task shows live `llm_stream_progress` updates without page reload. |
| Tests — repro | `services/worker-service/tests/test_long_output_no_timeout.py` (new) | new | Recorded-Bedrock-fixture or stub demonstrates: a model that streams 5k tokens at 50 tok/s completes successfully; a model that exceeds `maxTokens` surfaces `stopReason=max_tokens` rather than `ReadTimeoutError`. |
| TEMP debug cleanup | `services/worker-service/executor/graph.py` (`TEMP_DEBUG_BEDROCK` markers) | removal | Strip the diagnostic logging added during investigation once Task 4's structured `llm_stream_progress` entries are in place. |

---

## A3. Dependency Graph

```
Task 1 (Transport defaults + resolver)──────────────┐
          │                                         │
          ▼                                         │
Task 2 (Real boto3 timeout in providers.py)         │
          │                                         │
          ▼                                         │
Task 3 (maxTokens in providers.py + ChatBedrockConverse direct construction)
          │
          ▼
Task 4 (Streaming via astream + chunk-merge in agent_node)
          │
          ▼
Task 5 (llm_stream_progress / _complete conversation log entries)
          │
          ▼
Task 6 (Console render branch for streaming entries — browser-verified)

Task 7 (API agent_config.llm_transport sub-object — Java) ──► (parallel; Task 1's resolver consumes it)

Task 8 (System prompt: chunked-artifact guidance) ──► (parallel; independent)

Task 9 (Repro test + TEMP_DEBUG_BEDROCK cleanup) ──► (after Tasks 1-8)
```

Tasks 1, 7, 8 can start in parallel. Task 7 must merge before Task 1 can consume the agent-config field, but Task 1 can land first with platform defaults and the agent-config wiring added in a follow-up commit. Tasks 2–6 are sequential because they share the same call site in `graph.py`. Task 9 closes out and removes investigation scaffolding.

---

## A4. Acceptance Criteria (mirrors #85)

- [ ] Worker startup logs no longer emit any langchain `"... was transferred to model_kwargs"` warning for **any** of the three providers (Bedrock, OpenAI, Anthropic).
- [ ] Unit tests assert the configured `read_timeout` is actually applied:
  - Bedrock: boto3 client's `meta.config.read_timeout` (or equivalent — verify the stable read path interactively first) equals the configured value.
  - OpenAI: constructed `ChatOpenAI.request_timeout` equals the configured value.
  - Anthropic: constructed `ChatAnthropic.default_request_timeout` equals the configured value.
- [ ] Worker emits a structured `llm_stream_progress` entry on the **first chunk arrival** (typically <1 s) so operators see liveness immediately. Subsequent progress entries are throttled to one every 10 s wall time. A successful call always emits exactly one terminal `llm_stream_complete`. (For very fast calls that complete in <10 s, this means: 1 progress entry on first chunk + 1 complete entry — never zero progress entries.)
- [ ] `inferenceConfig.maxTokens` is present in the Bedrock request payload (verify via stub or recorded fixture). When the model would exceed it, the worker emits a structured warning `llm.max_tokens_reached` with `model`, `max_tokens`, and the resulting `AIMessage`'s `response_metadata.stopReason`.
- [ ] Console task page shows live progress during a generation ≥ 30 s (browser-verified per `docs/CONSOLE_TASK_CHECKLIST.md`).
- [ ] Re-running the prompt from #85 (`Help me research on all features of aws bedrock…`) on the same agent + GLM-5 either succeeds with a complete report or fails fast with a `max_tokens_reached` warning citing the cap — never silently times out at ~300 s.
- [ ] Default system prompt for agents whose allowed-tools list contains `create_text_artifact` includes chunked-output guidance (asserted in unit test).
- [ ] All `TEMP_DEBUG_BEDROCK` markers removed from `services/worker-service/executor/graph.py`.

---

## A5. Out of Scope (explicit non-goals)

- **Switching the default model** away from GLM-5 — that is a per-agent tuning decision, not a runtime fix. The runtime must be correct under slow models.
- **Adding a task-level retry-cap exemption** for `ReadTimeoutError` specifically — once the streaming + maxTokens fixes land, this error class is rare and the existing retry budget is appropriate.
- **Per-tool-argument size enforcement** — tempting (cap tool arguments mid-stream and abort) but the right place to bound this is `maxTokens` (Task 3) plus prompt guidance (Task 8), not custom interception logic.
- **Streaming partial tool calls back to the agent's tool executor** — out of scope. We materialize the full `AIMessage` before the tool-execution node runs, exactly as today.
- *(removed — streaming and transport configuration are now provider-agnostic; see Task 4 and Task 2+3 respectively)*

---

## A6. Risks

| Risk | Mitigation |
|---|---|
| `astream` chunk-merge subtly differs from `ainvoke` final message (e.g., `usage_metadata` arrives only on the last chunk; `tool_call_chunks` vs `tool_calls`) | Recorded-fixture parity test in Task 4: same input must produce structurally equivalent `AIMessage` between `ainvoke` and chunk-merged `astream`. |
| Per-chunk progress events flood the conversation log on fast models | Task 5 throttles to one entry per 10 s wall time AND minimum 200 chunks elapsed; terminal `llm_stream_complete` is always one entry. |
| `maxTokens` cap truncates a legitimate long response → user-visible mid-sentence cut-off | Default 16 384 sized for the worst-case observed (7k tool-use, 5k text). Surface `stopReason=max_tokens` prominently in Console so the operator can raise the cap or chunk via Task 8's prompt guidance. |
| Console rendering branch for streaming entries adds visual noise | Render as a single rolling line that collapses into the LLM-response card on `llm_stream_complete` — no permanent timeline entry per progress tick. |
| Breaking change for any external consumer of conversation log entries | New `kind` values are additive; existing consumers see them as unknown and ignore (verified by grep over Console + API). |

---

## A7. Sequencing Notes

- Tasks 2 + 3 are bundled in one PR — they're the same file (`providers.py`) and the rewrite from `init_chat_model` to direct `ChatBedrockConverse` construction would be incomplete if either landed alone.
- Task 4 (streaming) must merge before Task 5 (progress events) — there's nothing to emit progress about under `ainvoke`.
- Task 6 (Console) must merge with Playwright verification per `docs/CONSOLE_TASK_CHECKLIST.md` (the orchestrator runs Playwright after the subagent ships unit tests, per `feedback_playwright_enforcement.md`).
- Task 8 (system prompt) can be reviewed and merged in isolation; its acceptance criterion is unit-test-only.
- Task 9 (cleanup) is the merge gate for closing #85.
