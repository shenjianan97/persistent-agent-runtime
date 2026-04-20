# Progress — LLM Transport Hardening

Tracking [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85). One row per task; update inline as work progresses.

| # | Task | Owner | Status | Notes |
|---|---|---|---|---|
| 1 | Transport defaults + resolver (`executor/transport.py`) |  | Not started | Platform defaults: `connect=10s`, `read=120s`, `max_tokens=16_384`. Resolver merges agent-config overrides. |
| 2 | Real boto3 timeout in `providers.py` |  | Not started | Construct `ChatBedrockConverse` directly with `botocore.Config`; eliminate the `init_chat_model` warning. **Bundle with Task 3 in one PR.** |
| 3 | `max_tokens` wired through `providers.py` |  | Not started | Pass `max_tokens` field on `ChatBedrockConverse`. Same PR as Task 2. |
| 4 | Streaming via `astream` in `agent_node` |  | Not started | Replace `ainvoke` at `executor/graph.py:1173`. Chunk-merge into `AIMessage`. Recorded-fixture parity test required. |
| 5 | `llm_stream_progress` / `_complete` conversation log entries |  | Not started | New `ConversationLogKind` literals. 10-second throttle. Idempotency-key per checkpoint + sequence. |
| 6 | Console render branch for streaming entries |  | Not started | Browser-verified per `docs/CONSOLE_TASK_CHECKLIST.md` by the orchestrator. |
| 7 | API agent_config `llm_transport` sub-object (Java) |  | Not started | Parallel with Task 1. Validation bounds documented in plan §A2. |
| 8 | System prompt: chunked-artifact guidance |  | Not started | Independent. Unit-test acceptance only. |
| 9 | Repro test + `TEMP_DEBUG_BEDROCK` cleanup |  | Not started | Closes #85. Must run after Tasks 1–8. |
