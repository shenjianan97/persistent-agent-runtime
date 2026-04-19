<!-- AGENT_TASK_START: task-5-worker-embeddings.md -->

# Task 5 — Worker Embedding Provider Integration

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-5-memory.md` — section "Embeddings" end-to-end.
2. `services/model-discovery/` — startup script that validates chat-model provider keys today. The embedding key is validated on the same path.
3. `services/worker-service/executor/providers.py` — how the worker reads chat-model provider credentials; apply the same pattern for the embedding provider.
4. `services/worker-service/tools/providers/search.py` — an example of a provider-backed helper module in the worker.
5. `services/worker-service/core/db.py` — the asyncpg pool pattern used by the worker.
6. Phase-1 `provider_keys` schema (migrations 0001 / 0003) — the embedding provider key lands in the same table.

**CRITICAL POST-WORK:** After completing this task:
1. Run `make worker-test` and `make test` (model-discovery covered in unit tests). Fix any regressions.
2. Update the status in `docs/exec-plans/active/phase-2/track-5/progress.md` to "Done".

## Context

The worker needs to compute embeddings at two distinct points:

1. **Write time** — inside the `memory_write` LangGraph node (Task 6) and the dead-letter memory hook (Task 8), to populate `content_vec` on the outgoing row.
2. **Tool time** — inside the `memory_search` tool (Task 7) when `mode` requires a vector branch (the tool delegates to the Java REST API, so the worker side does NOT need a vector call there — the API side does, handled in Task 3).

In practice, the Python-side helper only needs to cover the write path. This task delivers:

- An `embeddings.py` helper exposing a single async function `compute_embedding(text: str) -> list[float] | None`.
- An extension to the model-discovery startup path so the embedding provider key is validated alongside chat-model keys at process start. Invalid / absent key → startup fails fast with a clear message when any memory-enabled agent exists; otherwise → startup logs a warning and continues (memory-disabled agents still operate).
- A deferred-embedding fallback: if `compute_embedding` returns `None`, the caller writes the row with `content_vec = NULL`. Do NOT raise — the invariant "one entry per completed memory-enabled task" must hold.

## Task-Specific Shared Contract

- **Model and dimension:** platform default is `text-embedding-3-small`, 1536 dimensions. Hard-coded in v1 — do not parameterise.
- **Provider credential path:** the embedding provider uses the same `provider_keys` mechanism Phase 2 preserved (raw key in Postgres; Secrets-Manager migration is Phase 3+). If the embedding provider shares the chat provider (e.g., both OpenAI), reuse the existing row; if it is a separate provider, a new row is expected in `provider_keys`.
- **Concurrency:** `compute_embedding` must be safe to call concurrently from multiple tasks. Use an `httpx.AsyncClient` bounded by the existing worker HTTP client factory if one exists; otherwise create one lazily per call (don't hold a global client — the worker process lifecycle is long, but connection reuse is less important than simplicity here).
- **Timeout and retries:** 5-second timeout; up to 1 retry on connection / 5xx. On exhaustion, return `None` (do NOT raise).
- **Return shape:** `list[float]` of length 1536 on success; `None` on any failure.
- **No logging of the input text** — the text may carry sensitive content. Log only `tokens`, `latency_ms`, and `cost_microdollars` on success; log `error_class` and `error_message_short` on failure.
- **Cost accounting:** the caller (Task 6 / Task 8) records cost in `agent_cost_ledger` attributed to the task's current checkpoint. This helper does NOT write to the ledger — it only returns the vector and exposes the token count via a small metadata object (see return type below).
- **Token counting:** use the provider's response's `usage.prompt_tokens` (or equivalent field). If unavailable, approximate as `ceil(len(text) / 4)` and log that the count is an approximation.

### Suggested return type

Because callers need the vector AND the token count for ledger writes, consider returning a small dataclass:

```
@dataclass
class EmbeddingResult:
    vector: list[float]        # length 1536
    tokens: int                # prompt tokens consumed
    cost_microdollars: int     # computed from a pricing helper (see below)
```

`compute_embedding(text) -> EmbeddingResult | None` — `None` on any failure, exception handled internally.

### Pricing helper

Use the existing `models` table (Track 1) if the embedding model has a row with `input_price_microdollars_per_mtok`. Otherwise use a hard-coded constant in a comment pointing at the provider's published rate card. The exact number is not load-bearing in v1 because embeddings are zero-rated — see design doc "Embeddings → Cost accounting".

## Affected Component

- **Service/Module:** Worker Service + Model Discovery
- **File paths:**
  - `services/worker-service/executor/embeddings.py` (new — `compute_embedding` + `EmbeddingResult`)
  - `services/worker-service/tests/test_embeddings.py` (new)
  - `services/model-discovery/*` (modify — validate the embedding key at startup alongside chat-model keys; add its row to `models` if the existing schema accommodates `kind='embedding'`)
  - `services/worker-service/core/config.py` (modify if needed — expose the embedding model id / dimension as module constants)
- **Change type:** new helper + modification to discovery

## Dependencies

- **Must complete first:** Task 1 (pgvector image pin ensures the test DB can hold `vector(1536)` — relevant when the helper is smoke-tested end-to-end).
- **Provides output to:** Task 6 (write path), Task 8 (dead-letter hook).
- **Shared interfaces/contracts:** `compute_embedding(text) -> EmbeddingResult | None` and the model-discovery-surfaced startup behaviour.

## Implementation Specification

### `compute_embedding` helper

- Async function.
- Reads the embedding provider's key from `provider_keys` via the existing worker helper path (same mechanism as chat-model provider reads).
- Calls the embedding endpoint over HTTPS with the configured timeout / retry budget.
- On success: returns `EmbeddingResult(vector, tokens, cost_microdollars)`. Verify `len(vector) == 1536`; reject (return `None`) otherwise.
- On failure: logs `memory.embedding.failed` with `error_class` and a short message; returns `None`.
- No background retry / no async queueing. A failed call is simply `None`; the caller writes with `NULL` vector.

### Model-discovery integration

- At startup, the existing `provider_keys` validation path iterates known providers. Extend it to include the embedding provider as a separate entry.
- Perform a cheap embed-a-probe call (e.g., `"healthcheck"`) to verify the key works.
- On failure:
  - If any `agents.agent_config->memory->>'enabled' = 'true'` row exists in the DB (memory is actually in use), fail startup with a clear message ("embedding provider key invalid or unreachable — required because N memory-enabled agent(s) exist").
  - Otherwise, log a WARN structured line and continue. Memory-disabled agents are unaffected.
- Record the model in `models` with `kind='embedding'` (or the existing discriminator; add one if the current schema does not differentiate chat vs embedding — if adding a discriminator is required, coordinate with Task 1 to land that in `0011_agent_memory.sql`).

### Deferred-embedding path

The helper never raises. Callers handle `None` by writing `content_vec = NULL`. Task 6 and Task 8 each log a `memory.embedding.deferred` structured line when this happens; this task only ensures the helper honours the contract.

### Local development UX

- Add a one-line README note (`services/worker-service/README.md`) describing that an embedding provider key is now required for memory-enabled agents, and where to set it locally (mirroring the existing chat-model setup).
- If `.env.example` is used, add the new env var name with a placeholder.

## Acceptance Criteria

- [ ] `compute_embedding("hello world")` returns an `EmbeddingResult` with `len(vector) == 1536` on a healthy provider.
- [ ] `compute_embedding("hello world")` returns `None` when the provider is unreachable (mock / stub) — without raising.
- [ ] `compute_embedding` emits `memory.embedding.succeeded` on success and `memory.embedding.failed` on failure, neither log line containing the input text.
- [ ] Model-discovery startup fails fast when the embedding key is invalid AND any memory-enabled agent exists.
- [ ] Model-discovery startup logs a warning and continues when the embedding key is invalid AND no memory-enabled agent exists.
- [ ] Model-discovery upserts a row into `models` for the embedding model on success.
- [ ] Local README / `.env.example` documents the new variable / key.
- [ ] All new unit tests pass; worker unit tests unchanged.

## Testing Requirements

- **Unit tests:**
  - `compute_embedding` happy-path returns `EmbeddingResult` with correct dimension.
  - Timeout → returns `None`.
  - 5xx → retries once, then returns `None`.
  - Provider returns a vector of wrong dimension → returns `None`.
  - No test captures the input text in assertions on log output (enforces the "no input text in logs" contract).
- **Model-discovery tests:** happy path upserts the embedding row; missing key + memory-enabled agents present → startup fails with a diagnostic message; missing key + no memory-enabled agents → startup continues with a warning.
- **No E2E required here** — Tasks 6, 7, 8 exercise the helper end-to-end. Pure unit tests for this task.

## Constraints and Guardrails

- Do not parameterise the embedding model or dimension — both are hard-coded to `text-embedding-3-small` / 1536.
- Do not introduce a background backfill worker for deferred embeddings — explicitly out of scope in v1.
- Do not change how chat-model provider keys are read or validated.
- Do not compute cost from a source other than the `models` table or a documented hard-coded constant — do not inline a magic number without a comment citing the provider's price rate card.
- Do not log the input text anywhere, including at DEBUG. The text can carry PII.
- Do not attempt to normalise / canonicalise text before embedding — send exactly what the caller supplies.
- Do not add caching of embeddings by text hash in v1 — the memory write path computes once per task; the search path is Java-side.

## Assumptions

- The worker can read `provider_keys` rows via the existing helper (same as chat-model provider reads).
- The embedding provider supports a simple `POST /v1/embeddings` shape or equivalent — no MCP / streaming protocol.
- The worker process is long-lived; startup-time validation in model-discovery is the enforcement point, not a periodic re-check.
- `text-embedding-3-small` is the supported default; any future change is a separate track.
- The `models` table tolerates embedding rows (coordinate with Task 1 / model-discovery if a `kind` discriminator is needed).

<!-- AGENT_TASK_END: task-5-worker-embeddings.md -->
