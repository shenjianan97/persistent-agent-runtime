<!-- AGENT_TASK_START: task-4-tool-result-offload.md -->

# Task 4 — Tier 0 Ingestion Offload: Tool Results AND Tool-Call Args (S3-Backed)

## Agent Instructions

**CRITICAL PRE-WORK:**
1. Read GH issue [#83](https://github.com/shenjianan/persistent-agent-runtime/issues/83) in full — production context, design rationale, explicit non-goals.
2. Grep to anchor the real file locations (prior specs had stale names):
   - `grep -rn "def _get_tools\|def _wrap_tool" services/worker-service/executor/` — the tool-execution wrapper site Track 7 Task 4 hooked into.
   - `grep -rn "head+tail\|25KB\|25_000\|PER_TOOL_RESULT_CAP_BYTES" services/worker-service/` — the head+tail 25KB trim code this task REPLACES.
   - `grep -rn "truncate_tool_call_args" services/worker-service/executor/compaction/` — the Tier 1.5 transform this task DELETES.
3. Read `services/worker-service/storage/s3_client.py` — existing `S3Client` with async `upload` / `download` over the `platform-artifacts` bucket. Reuse; do not create a new client.
4. Read the tool-wrapping site in `services/worker-service/executor/graph.py` where the current Tier 0 cap is applied — this is where result offload hooks in.
5. Read the AIMessage-append site in `graph.py` (where the LLM response is appended to `state["messages"]`) — this is where arg offload hooks in.

**CRITICAL POST-WORK:**
1. Run `make worker-test` and `make api-test`. All tests green.
2. Update `progress.md` to mark Task 4 Done.

---

## Context

Track 7 landed a reactive three-tier in-place compactor. The follow-up architecture pivot ("replace-and-rehydrate") drops Tier 1 (tool-result clearing) and Tier 1.5 (arg truncation) and moves their byte-bounding responsibilities **up to ingestion time**. Two wins:

- `state["messages"]` stays nearly append-only (Task 3's `pre_model_hook` does projection; only Task 5's Option C ever mutates).
- Every journal entry is byte-bounded at the moment it lands, so Task 3's projection over arbitrary historical slices is always cheap.

Track 7's Tier 0 head+tail 25KB trim (lossy, irrecoverable) is REPLACED by offload-with-preview (lossless — original content still in S3; the agent retrieves via Task 5's `recall_tool_result`).

## Goal

At the exact moment a large `ToolMessage.content` or a large `AIMessage.tool_calls[*].args[<truncatable-key>]` is about to be appended to `state["messages"]`, persist the full content to S3 and replace it in state with a URI + short preview. Below the threshold, content stays inline. No other pipeline stage offloads.

## Contract — Behaviour Changes

### 1. `ToolResultArtifactStore` abstraction

New module `services/worker-service/executor/compaction/tool_result_store.py` (keep the filename even though it now covers arg content too — the abstraction is unchanged).

- `ToolResultURI` dataclass — components:
  - Tool-RESULT URI: `(tenant_id, task_id, tool_call_id, content_hash)` → `toolresult://{tenant_id}/{task_id}/{tool_call_id}/{content_hash}.txt`.
  - Tool-ARG URI: `(tenant_id, task_id, tool_call_id, arg_key, content_hash)` → `toolresult://{tenant_id}/{task_id}/{tool_call_id}/args/{arg_key}/{content_hash}.txt`.
  - `content_hash` is the first 12 hex chars of `sha256(content_bytes)`.
- `parse_tool_result_uri(s: str) -> ToolResultURI` — raises `ValueError` on malformed input. Validates scheme, tenant/task/tool-call components non-empty, hash shape. Agent-supplied URIs are untrusted; Task 5's recall tool relies on this parser rejecting bad shape.
- `ToolResultArtifactStore` abstract base:
  - `async put(*, tenant_id, task_id, tool_call_id, content: str, arg_key: str | None = None) -> str` returns URI. Computes `content_hash` internally. When `arg_key` is provided, uses the arg URI scheme.
  - `async get(uri: str) -> str | None` returns content, `None` on **missing key only** (S3 `NoSuchKey` / 404). Transport / auth / other backend errors MUST raise — Task 5 distinguishes these from retention GC.
- `S3ToolResultStore(s3_client)` — production. Key scheme matches the URI path under the `platform-artifacts` bucket. Content-Type `text/plain; charset=utf-8`.
- `InMemoryToolResultStore` — dict-backed, for unit tests.

**Why the content hash is load-bearing.** `tool_call_id` is NOT unique across provider-level retries (Bedrock can reuse a `tooluse_*` id with different content). Content hash in the key means a replay doesn't overwrite an earlier offload; each lands at its own URI and earlier placeholders still resolve to the content they were captured against.

**Non-mutation contract.** `put(content)` MUST NOT mutate its `content` argument, any caller-held reference, or any LangGraph state. The store reads the string, hashes it, writes to S3, returns the URI. Nothing else.

### 2. Tier 0 ingestion offload — tool RESULTS

Wired into the tool-execution wrapper in `graph.py` (same site Track 7's Tier 0 cap lives today — grep for the 25KB trim code to find it).

- Right after the tool returns and before the `ToolMessage` is appended to `state["messages"]`:
  - If `len(content.encode("utf-8")) > OFFLOAD_THRESHOLD_BYTES` (default 20_000):
    1. `uri = await store.put(tenant_id, task_id, tool_call_id, content)`.
    2. Build a preview (first ~5 lines OR first ~500 bytes, whichever shorter; UTF-8-safe truncation).
    3. Replace content in state with `"[tool result {N} bytes @ {uri} preview: {preview}]"`.
  - Else: store inline; no S3 write.
- **Completely supersedes** Track 7's "head+tail 25KB trim". The old trim code (grep'd in pre-work) is DELETED. The constant `PER_TOOL_RESULT_CAP_BYTES = 25_000` is also removed.

### 3. Tier 0 ingestion offload — tool-call ARGS

Wired at the AIMessage-append site in `graph.py` — the moment an `AIMessage` with `tool_calls` returned by the LLM is about to land in `state["messages"]` (and before the tool is invoked, so downstream tools receive the reference-replaced args).

- Walk `tool_calls[*].args` for each call. For any key in `TRUNCATABLE_ARG_KEYS = {"content", "new_string", "old_string", "text", "body"}` (same set as Track 7 Tier 1.5) whose value is a string with `len(value.encode("utf-8")) > OFFLOAD_THRESHOLD_BYTES`:
  1. `uri = await store.put(tenant_id, task_id, tool_call_id, content=value, arg_key=<key>)`.
  2. Build a preview (same rule as §2).
  3. Replace the value in the dict with `"[tool arg '{key}' {N} bytes @ {uri} preview: {preview}]"`.
- Non-truncatable keys (e.g. `search_phrase`, `path`, `query`) are NEVER offloaded, even if oversized. Track 7's precedent for the key allowlist carries forward unchanged.
- **Replaces** Track 7 Tier 1.5. `truncate_tool_call_args` in `executor/compaction/transforms.py` is DELETED as part of this task (ingestion-time arg handling replaces reactive arg handling at compaction time).

### 4. Per-candidate fail-closed semantics

- If `store.put` raises for a given ToolMessage or for a given tool_call arg:
  - That item is NOT offloaded AND its content stays INLINE in `state["messages"]` (the raise happened before the replacement, so nothing was mutated).
  - Log `compaction.offload_failed` at WARN with `tool_call_id`, `tool_name` (or `arg_key` for the arg path), `error_type`, `error_message[:200]`.
  - Increment a Prometheus counter (one label dimension for result vs arg).
- If ALL offload attempts on a single ingestion pass raise: emit `compaction.offload_all_failed` WARN once for the pass. Agent execution proceeds with inline content — the worker is still functional; S3 health is the operator's problem. No state mutation, no raise.

### 5. Config flag `context_management.offload_tool_results`

- New boolean field on the existing `context_management` sub-object. Default `true`.
- Jackson mapping in `ContextManagementConfigRequest.java` with `@JsonProperty("offload_tool_results")`. Canonicalisation in the existing `context_management` round-trip site (Track 7 Task 1 pattern).
- Validation: null-tolerant bool in `ConfigValidationHelper.java`.
- When `false`, BOTH the result offload path (§2) AND the arg offload path (§3) are disabled. Inline storage is used regardless of size — matches pre-Task-4 behaviour. This is the operator's kill switch.
- **Explicitly NOT Console-editable in v1.** Console TypeScript adds the optional field (round-trip stability), but no form renderer, no toggle. Operators who need to disable per-agent use the agent-update API directly. Coverage matrix does NOT need an update (field is not rendered).

### 6. Idempotency across pipeline re-entries

Ingestion offload runs at tool-execution / LLM-response time — exactly once per ToolMessage or AIMessage appended to state. It does NOT run from inside `pre_model_hook`. The hook reads whatever is already in `state["messages"]` and projects a view.

If the same `(tenant_id, task_id, tool_call_id)` sees content twice (provider retry): content_hash in the key disambiguates. Two different contents produce two different URIs — replay-safe.

## Affected Files

**Before implementing: grep to confirm.** The wrapper / append sites below are best-guess and prior specs have been wrong.

- `services/worker-service/executor/compaction/tool_result_store.py` — **new file**. Store abstraction + `S3ToolResultStore` + `InMemoryToolResultStore` + URI dataclass + parser.
- `services/worker-service/executor/compaction/caps.py` (if that's where the Track 7 Tier-0 cap lives — grep to confirm) — REPLACE head+tail trim with offload; if the cap logic lives inline in `graph.py`, edit there instead.
- `services/worker-service/executor/compaction/transforms.py` — DELETE `truncate_tool_call_args` and its helpers. (Track 7's `clear_tool_results` is deleted by Task 3, not here.)
- `services/worker-service/executor/graph.py` — wire ingestion offload into the tool-execution wrapper (results, §2) and the AIMessage-append site (args, §3). Instantiate `S3ToolResultStore` from the existing `S3Client`. Read the `offload_tool_results` flag off agent config and gate both paths.
- `services/worker-service/executor/compaction/defaults.py` — add `OFFLOAD_THRESHOLD_BYTES = 20_000` and `TRUNCATABLE_ARG_KEYS = frozenset({"content", "new_string", "old_string", "text", "body"})`. DELETE `PER_TOOL_RESULT_CAP_BYTES` (old 25_000 head+tail constant).
- `services/api-service/src/main/java/.../request/ContextManagementConfigRequest.java` — new `offloadToolResults` boolean, Jackson `@JsonProperty("offload_tool_results")`, default `true`.
- `services/api-service/src/main/java/.../service/ConfigValidationHelper.java` — validation (null-tolerant bool).
- `services/console/src/types/` — add optional `offloadToolResults?: boolean` to the `ContextManagement` type. Grep to confirm the file (`grep -rn "context_management\|ContextManagement" services/console/src/types/`). No renderer.
- `services/worker-service/tests/test_tool_result_store.py` — **new**. URI round-trip (result + arg variants), put/get, idempotent hash, missing-key → None, transport-error → raise, non-mutation, hash-disambiguates-retry.
- `services/worker-service/tests/test_compaction_ingestion_offload.py` — **new**. Result offload fires at threshold, arg offload fires at threshold for truncatable keys only, below-threshold stays inline, fail-closed per-item, all-failed case, config flag disables both paths.

## Dependencies

None at code level. Landing order is 2 → **4** → 3 → 5. Task 3 assumes Task 4's byte-bounded journal entries; Task 5 reads from the store Task 4 creates.

## Out of Scope for This Task

- Proactive pre-compute offloads (always-offload for certain tool names, scheduled compression passes). Everything here is reactive to ingestion events.
- Cross-task / cross-tenant artifact sharing — blocked by the task-scope URI validation in Task 5.
- Compression of stored content.
- Retention / lifecycle rules on the S3 side — the existing `platform-artifacts` bucket policy applies.
- The `recall_tool_result` agent-facing tool — Task 5.
- System-prompt hint about the new capability — Task 5.
- `offload_emitted` conversation-log event — Task 5 (consolidates agent-facing wiring there).

## Acceptance Criteria (observable behaviours)

1. Unit: `S3ToolResultStore.put(content=15KB)` writes under a deterministic key containing `tenant_id`, `task_id`, `tool_call_id`, and content hash. `store.get(uri)` round-trips byte-for-byte.
2. Unit: `store.get` on a URI that doesn't exist returns `None` (not raise). Only `NoSuchKey` maps to `None`.
3. Unit: `store.get` on transport/auth/other backend errors **raises** (no silent swallowing). Task 5's recall tool distinguishes this case from retention GC.
4. Unit: `store.put(content=X)` does not mutate `X` or any caller-held reference. Assert post-call identity AND value.
5. Unit: `store.put(same tool_call_id, different content)` → TWO DIFFERENT URIs (content hash disambiguates). Both readable via `get`.
6. Unit: `store.put(arg_key="content", ...)` produces a URI whose path includes `/args/content/` and round-trips correctly.
7. Graph test: tool returning 25KB content → `store.put` called once; the resulting ToolMessage in `state["messages"]` has the `"[tool result N bytes @ ... preview: ...]"` form and is length-bounded (no 25KB in state).
8. Graph test: tool returning 5KB content → `store.put` NOT called; ToolMessage stored inline verbatim.
9. Graph test: AIMessage with `tool_calls[0].args["content"]` of 30KB → `store.put(arg_key="content")` called once; the value in the stored AIMessage becomes the `"[tool arg 'content' N bytes @ ... preview: ...]"` form.
10. Graph test: AIMessage with `tool_calls[0].args["search_phrase"]` of 30KB → NOT offloaded (non-truncatable key). Value stored inline. Only `{content, new_string, old_string, text, body}` are candidates.
11. Graph test: `context_management.offload_tool_results = false` → neither §2 nor §3 fires; both results and args stored inline regardless of size. `store.put` never called.
12. Fail-closed (partial): two oversized tool results, `store.put` raises on one → the failing one stays inline, the other is offloaded normally. `compaction.offload_failed` WARN emitted once with the failing `tool_call_id`.
13. Fail-closed (all): both oversized tool results raise on `store.put` → both stay inline, `compaction.offload_all_failed` WARN emitted once, pipeline proceeds without raising.
14. Regression: prior Track-7 tests that asserted "head+tail 25KB trim" behaviour either pass against the new offload logic (if functionally compatible — i.e. the content is no longer 25KB in state) or are DELETED with a one-line commit message noting the Tier 0 mechanism change.
15. Regression: `truncate_tool_call_args` is gone from `transforms.py`; any tests that imported it are deleted; no remaining import of the symbol anywhere in the worker.

## Pattern references in existing code

- `S3Client` wiring: `services/worker-service/storage/s3_client.py`.
- Tool-wrapping site Track 7 Task 4 hooked into: grep `_get_tools` / `_wrap_tool` in `executor/graph.py`.
- Config-flag validation precedent: `services/api-service/.../service/ConfigValidationHelper.java` (Track 7 Task 1).
- Placeholder + preview format precedent: Track 7 Task 5's `"[tool output not retained — ...]"` string. We're changing the format to include the URI inline and a preview; this is the closest prior art for the placeholder shape.

<!-- AGENT_TASK_END -->
