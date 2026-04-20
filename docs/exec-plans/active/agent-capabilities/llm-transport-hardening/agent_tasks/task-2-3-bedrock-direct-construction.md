<!-- AGENT_TASK_START: task-2-3-bedrock-direct-construction.md -->

# Task 2 + 3 — Direct `ChatBedrockConverse` Construction with Real Timeout + `max_tokens`

> Tasks 2 and 3 are bundled because they touch the same ~20 lines in `executor/providers.py`; landing them separately would leave the file in an inconsistent state.

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — root cause: `init_chat_model` silently moves `timeout` and `max_retries` into `model_kwargs`. Confirmed via langchain warning at worker startup.
3. `services/worker-service/executor/providers.py` (entire file) — the call site to rewrite.
4. `services/worker-service/executor/transport.py` — Task 1's resolver. This task's signature changes to consume it.
5. `langchain_aws.chat_models.bedrock_converse.ChatBedrockConverse` source — confirmed accepts `client`, `config`, `max_tokens`. This task uses all three.
6. botocore `Config` documentation, specifically `read_timeout`, `connect_timeout`, `retries={"max_attempts": ...}`.

**CRITICAL POST-WORK:** After completing this task:
1. Restart the worker (`make stop && make start`) and confirm worker startup logs **no longer** contain `"timeout was transferred to model_kwargs"` or `"max_retries was transferred to model_kwargs"`.
2. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_providers_transport.py -v`.
3. Update `progress.md` rows 2 and 3 to "Done".

## Context

Direct evidence from #85's investigation:

```
WARNING! timeout is not default parameter. timeout was transferred to model_kwargs.
WARNING! max_retries is not default parameter. max_retries was transferred to model_kwargs.
```

`init_chat_model("bedrock_converse", timeout=300, max_retries=0)` does not validate kwargs against the underlying model class — it silently moves unknowns into `model_kwargs`. For Bedrock those become Converse *inference* parameters and have no effect on the boto3 client. The visible 300 s ceiling we observed during the investigation was botocore's default `read_timeout`, not anything we configured.

The fix for Bedrock is to construct `ChatBedrockConverse` directly with a pre-built `boto3.client("bedrock-runtime", config=botocore.Config(...))` and `max_tokens=...`. OpenAI and Anthropic providers in this file have their own analogous defects (langchain warnings emit there too); this task fixes Bedrock and explicitly leaves the other providers' transport tuning for a follow-up task — but does remove the silently-dropped kwargs from those code paths so the warnings disappear.

## Task-Specific Shared Contract

- `create_llm` signature is extended to require the resolver's output: `create_llm(pool, provider, model_name, temperature, transport: LLMTransportConfig) -> BaseChatModel`. Callers (`graph.py`) update to pass it.
- For `provider == "bedrock"`:
  - Build `botocore.config.Config(connect_timeout=transport.connect_timeout_s, read_timeout=transport.read_timeout_s, retries={"max_attempts": 0})` and a `boto3.client("bedrock-runtime", region_name=region, config=config)` once per `create_llm` call.
  - Construct `ChatBedrockConverse(model=model_name, temperature=temperature, region_name=region, client=client, max_tokens=transport.max_output_tokens)`. Do **not** pass `timeout` or `max_retries`.
  - Bedrock auth: continue to honor the database-stored `api_key` by setting it via `os.environ["AWS_BEARER_TOKEN_BEDROCK"]` for the duration of client construction (preserve the current behavior — verify with the existing integration tests).
- For `provider in ("openai", "anthropic")`:
  - Stop passing `timeout=` and `max_retries=` to `init_chat_model`. Comment in code explains: "init_chat_model drops unknown kwargs into model_kwargs; provider-native client tuning is a follow-up." Issue link to #85.
  - `max_tokens` is passed via `init_chat_model`'s native `max_tokens` argument when supported; verify the warning does not fire (it should not, both LangChain `ChatOpenAI` and `ChatAnthropic` accept `max_tokens` natively).
- The function never re-fetches `api_key` more than once per call.
- `region` for Bedrock continues to come from `os.environ.get("AWS_BEDROCK_REGION", "us-east-1")` — unchanged.

## Affected Component

- **Service/Module:** Worker — Executor
- **File paths:**
  - `services/worker-service/executor/providers.py` (rewrite)
  - `services/worker-service/tests/test_providers_transport.py` (new)
  - `services/worker-service/executor/graph.py` (locate the single `create_llm(...)` call site and update it to pass the resolver output; do not touch the LLM call itself — that is Task 4)
- **Change type:** rewrite + new tests + small wiring change

## Dependencies

- **Must complete first:** Task 1 (transport resolver — the new dataclass).
- **Provides output to:** Task 4 (the constructed model is what `astream` will be called on; max_tokens must be set before then).
- **Shared interfaces/contracts:** `create_llm` signature + `LLMTransportConfig` consumption.

## Implementation Specification

### Rewrite: `executor/providers.py`

The body changes from `init_chat_model("bedrock_converse", timeout=300, max_retries=0, ...)` to direct `ChatBedrockConverse(client=...)` construction. The resolver-output is the *only* knob carrying timeouts and max_tokens — no hard-coded values remain in this file.

OpenAI / Anthropic branches keep `init_chat_model` but drop the silently-dropped kwargs. They consume `transport.max_output_tokens` via the provider-native `max_tokens` arg.

### Wiring change: `executor/graph.py`

Find the single `create_llm(...)` invocation. Add a call to `resolve_transport(agent_config, provider=..., model=...)` immediately above it; pass the result as the new `transport=` kwarg. Do not touch any other lines in this file (the streaming change is Task 4).

### Tests: `services/worker-service/tests/test_providers_transport.py`

Cover:

- For `provider == "bedrock"`:
  - The resulting `ChatBedrockConverse.client._endpoint.http_session` (or whichever boto3 attribute exposes the timeout — probe in a REPL first; the assertion contract is "the client's read_timeout equals what we passed") matches `transport.read_timeout_s`.
  - `max_tokens` field on the constructed model equals `transport.max_output_tokens`.
  - No `timeout` or `max_retries` keys appear in the model's `model_kwargs` (regression: prove the silent-drop bug is gone).
- For `provider == "openai"` and `"anthropic"`:
  - `max_tokens` is set via the native field (read it back from the model object).
  - `model_kwargs` does not contain `timeout` or `max_retries`.
- One smoke test: build a Bedrock model with the resolver defaults; invoke a tiny `.invoke([HumanMessage("hi")])` against a stub or mocked boto3 client and assert no exception.

### Caveat to validate during implementation

The exact attribute path on the boto3 client where `read_timeout` lives may differ across botocore versions. Before relying on a specific attribute in the test, the implementing agent should:

1. Build the client interactively in the worker venv.
2. Inspect `client._endpoint`, `client.meta.config`, etc. to find the right read path.
3. Pin the test against the most stable path (likely `client.meta.config.read_timeout` — verify).

If no stable read path exists, fall back to a black-box test: stub `boto3.client` and assert the `Config(...)` it was called with carries the expected `read_timeout` and `connect_timeout`.

## Acceptance Criteria

- [ ] Worker startup no longer emits langchain's `"... was transferred to model_kwargs"` warning for either provider.
- [ ] For Bedrock, the boto3 client's `read_timeout` equals the configured value (asserted in test).
- [ ] `max_tokens` is set on the constructed `ChatBedrockConverse` (asserted in test).
- [ ] `services/worker-service/tests/test_providers_transport.py` passes.
- [ ] No hard-coded timeout or max_tokens values remain in `providers.py` — all values flow through the resolver.

## Out of Scope

- Switching the LLM call to streaming (Task 4).
- Per-agent override wiring on the API side (Task 7).
- Bedrock-API-key rotation logic.

<!-- AGENT_TASK_END -->
