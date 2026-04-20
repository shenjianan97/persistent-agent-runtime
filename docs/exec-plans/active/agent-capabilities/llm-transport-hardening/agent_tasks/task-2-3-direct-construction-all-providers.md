<!-- AGENT_TASK_START: task-2-3-direct-construction-all-providers.md -->

# Task 2 + 3 — Direct Provider-Class Construction with Real Timeout + `max_tokens` (All Providers)

> Tasks 2 and 3 are bundled because they touch the same call site in `executor/providers.py`; landing them separately would leave the file in an inconsistent state. **Scope is all three providers** (Bedrock, OpenAI, Anthropic) — the same hazard applies to all of them, not just Bedrock.

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/exec-plans/active/agent-capabilities/llm-transport-hardening/plan.md` — overall architecture.
2. Issue [#85](https://github.com/shenjianan97/persistent-agent-runtime/issues/85) — root cause: `init_chat_model` silently moves unknown kwargs into `model_kwargs`. The Bedrock failure mode (`timeout` and `max_retries` warnings at startup) is the loudest example. The same hazard exists for OpenAI (native field is `request_timeout`, not `timeout`) and Anthropic (native field is `default_request_timeout`).
3. `services/worker-service/executor/providers.py` (entire file) — the call site to rewrite.
4. `services/worker-service/executor/transport.py` — Task 1's resolver. This task's signature changes to consume it.
5. Each provider's chat-model class fields (verified by `model_fields` inspection during plan authoring):
   - `langchain_aws.chat_models.bedrock_converse.ChatBedrockConverse` — accepts `client`, `config`, `max_tokens`.
   - `langchain_openai.ChatOpenAI` — accepts `request_timeout: float | tuple[float, float] | Any | None`, `max_retries: int | None`, `max_tokens: int | None`, `http_client`.
   - `langchain_anthropic.ChatAnthropic` — accepts `default_request_timeout: float | None`, `max_retries: int = 2`, `max_tokens: int | None`.
6. botocore `Config` documentation — specifically `read_timeout`, `connect_timeout`, `retries={"max_attempts": ...}`.

**CRITICAL POST-WORK:** After completing this task:
1. Restart the worker (`make stop && make start`) and confirm worker startup logs **no longer** contain any `"... was transferred to model_kwargs"` warning for any provider. (Trigger a task on each provider to force model construction.)
2. Run `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_providers_transport.py -v`.
3. Update `progress.md` rows 2 and 3 to "Done".

## Context

Direct evidence from #85's investigation showed Bedrock's failure clearly:

```
WARNING! timeout is not default parameter. timeout was transferred to model_kwargs.
WARNING! max_retries is not default parameter. max_retries was transferred to model_kwargs.
```

`init_chat_model("bedrock_converse", timeout=300, max_retries=0)` does not validate kwargs against the underlying model class — it silently moves unknowns into `model_kwargs`. For Bedrock those become Converse *inference* parameters and have no effect on the boto3 client. The visible 300 s ceiling we observed during the investigation was botocore's default `read_timeout`, not anything we configured.

**The same trap applies to OpenAI and Anthropic.** Their native field names differ — `request_timeout` and `default_request_timeout` respectively — and a caller using `timeout=` would land in the same silent-drop trap. We avoid relitigating this class of bug provider-by-provider by switching to direct per-provider class construction with provider-native field names. `init_chat_model` becomes optional indirection we no longer need; removing it makes the timeout we configure provably the timeout that applies.

## Task-Specific Shared Contract

- `create_llm` signature is extended to require the resolver's output: `create_llm(pool, provider, model_name, temperature, transport: LLMTransportConfig) -> BaseChatModel`. Callers (`graph.py`) update to pass it.

- **For `provider == "bedrock"`:**
  - Build `botocore.config.Config(connect_timeout=transport.connect_timeout_s, read_timeout=transport.read_timeout_s, retries={"max_attempts": 0})` and a `boto3.client("bedrock-runtime", region_name=region, config=config)` once per `create_llm` call.
  - Construct `ChatBedrockConverse(model=model_name, temperature=temperature, region_name=region, client=client, max_tokens=transport.max_output_tokens)`. Do **not** pass `timeout` or `max_retries` to the constructor.
  - Bedrock auth: continue to honor the database-stored `api_key` by setting it via `os.environ["AWS_BEARER_TOKEN_BEDROCK"]` for the duration of client construction (preserve the current behavior — verify with the existing integration tests).

- **For `provider == "openai"`:**
  - Construct `ChatOpenAI(model=model_name, temperature=temperature, api_key=api_key, request_timeout=transport.read_timeout_s, max_retries=0, max_tokens=transport.max_output_tokens)`.
  - Do **not** pass `timeout=` (silent drop) or `default_request_timeout=` (Anthropic's name; ChatOpenAI does not accept it).
  - `connect_timeout_s` is not separately configurable on `ChatOpenAI`'s default httpx transport without supplying a custom `http_client`. For v1 we accept httpx's default connect timeout (5 s). Document this inline; if customers need a tighter connect timeout, the follow-up is to thread an `httpx.AsyncClient(timeout=httpx.Timeout(connect=..., read=...))` into the `http_client` field. Out of scope for this task — but call it out in a code comment so the next maintainer doesn't re-discover it.

- **For `provider == "anthropic"`:**
  - Construct `ChatAnthropic(model=model_name, temperature=temperature, api_key=api_key, default_request_timeout=transport.read_timeout_s, max_retries=0, max_tokens=transport.max_output_tokens)`.
  - Same `connect_timeout_s` caveat as OpenAI: ChatAnthropic does not split connect vs read timeout in the current langchain wrapper. Document inline.

- The function never re-fetches `api_key` more than once per call.
- `region` for Bedrock continues to come from `os.environ.get("AWS_BEDROCK_REGION", "us-east-1")` — unchanged.
- **`init_chat_model` is no longer imported from this file.** Verify with `grep -n "init_chat_model" services/worker-service/executor/providers.py` after the rewrite — expect zero matches.

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

The body changes from `init_chat_model("bedrock_converse", timeout=300, max_retries=0, ...)` and the analogous OpenAI/Anthropic calls to direct provider-class construction. The resolver-output is the *only* knob carrying timeouts and max_tokens — no hard-coded values remain in this file. Each provider branch handles its native field-name quirks (Bedrock = boto3 client + botocore.Config; OpenAI = `request_timeout`; Anthropic = `default_request_timeout`).

### Wiring change: `executor/graph.py`

Find the single `create_llm(...)` invocation. Add a call to `resolve_transport(agent_config, provider=..., model=...)` immediately above it; pass the result as the new `transport=` kwarg. Do not touch any other lines in this file (the streaming change is Task 4).

### Tests: `services/worker-service/tests/test_providers_transport.py`

Cover all three providers with parallel structure:

- **For `provider == "bedrock"`:**
  - The resulting `ChatBedrockConverse.client.meta.config.read_timeout` (or whichever boto3 attribute exposes the timeout — probe in a REPL first; the assertion contract is "the client's read_timeout equals what we passed") matches `transport.read_timeout_s`.
  - `connect_timeout` likewise matches.
  - `max_tokens` field on the constructed model equals `transport.max_output_tokens`.
  - No `timeout` or `max_retries` keys appear in `model_kwargs` (regression: prove the silent-drop bug is gone).

- **For `provider == "openai"`:**
  - `ChatOpenAI.request_timeout` equals `transport.read_timeout_s`.
  - `ChatOpenAI.max_retries == 0`.
  - `ChatOpenAI.max_tokens == transport.max_output_tokens`.
  - `model_kwargs` does not contain `timeout`, `request_timeout`, `max_retries`, or `max_tokens` (those should all be on the model itself, not in `model_kwargs`).

- **For `provider == "anthropic"`:**
  - `ChatAnthropic.default_request_timeout` equals `transport.read_timeout_s`.
  - `ChatAnthropic.max_retries == 0`.
  - `ChatAnthropic.max_tokens == transport.max_output_tokens`.
  - `model_kwargs` does not contain `timeout`, `default_request_timeout`, `max_retries`, or `max_tokens`.

- **One smoke test per provider**: build the model with resolver defaults; invoke a tiny `.invoke([HumanMessage("hi")])` against a stub or mocked client and assert no exception (or skip with a clear marker if no stub is feasible — note the limitation in a comment).

- **Regression guard**: a `warnings.catch_warnings()`-wrapped construction for each provider asserts no `UserWarning` matching `r".*was transferred to model_kwargs.*"` is raised.

### Caveat to validate during implementation

The exact attribute path on the boto3 client where `read_timeout` lives may differ across botocore versions. Before relying on a specific attribute in the test, the implementing agent should:

1. Build the client interactively in the worker venv.
2. Inspect `client._endpoint`, `client.meta.config`, etc. to find the right read path.
3. Pin the test against the most stable path (likely `client.meta.config.read_timeout` — verify).

If no stable read path exists, fall back to a black-box test: stub `boto3.client` and assert the `Config(...)` it was called with carries the expected `read_timeout` and `connect_timeout`.

## Acceptance Criteria

- [ ] Worker startup no longer emits any langchain `"... was transferred to model_kwargs"` warning for any of the three providers.
- [ ] `init_chat_model` is no longer imported in `providers.py` (verified via grep).
- [ ] For each provider, the constructed model's native timeout field equals the resolver's `read_timeout_s` (asserted in test).
- [ ] For each provider, the constructed model's `max_tokens` equals the resolver's `max_output_tokens` (asserted in test).
- [ ] For each provider, `max_retries` on the constructed model is 0 (asserted in test).
- [ ] `services/worker-service/tests/test_providers_transport.py` passes.
- [ ] No hard-coded timeout or max_tokens values remain in `providers.py` — all values flow through the resolver.

## Out of Scope

- Switching the LLM call to streaming (Task 4).
- Per-agent override wiring on the API side (Task 7).
- Splitting connect vs read timeout for OpenAI / Anthropic via a custom httpx client (documented inline as future work; not required for this task).
- Bedrock-API-key rotation logic.

<!-- AGENT_TASK_END -->
