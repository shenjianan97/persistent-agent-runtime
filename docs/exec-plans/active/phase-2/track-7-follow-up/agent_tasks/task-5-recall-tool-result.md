<!-- AGENT_TASK_START: task-5-recall-tool-result.md -->

# Task 5 — Agent-Facing Recall Tool + System-Prompt Hint + Option C Reference-Replacement

## Agent Instructions

**CRITICAL PRE-WORK:**
1. **Task 4 AND Task 3 MUST be merged first.** Task 4 gives you the store + URI format + `offload_tool_results` flag. Task 3 gives you the `pre_model_hook` into which Option C reference-replacement lands.
2. Read GH issue [#83](https://github.com/shenjianan/persistent-agent-runtime/issues/83) — "Recall tool", "System-prompt hint", "Option C reference-replacement" sections.
3. Anchor file locations via grep (prior specs had stale names):
   - `grep -rn "def memory_note\|def memory_search" services/worker-service/` — existing built-in tool pattern with closure-bound tenant/task.
   - `grep -rn "pre_model_hook" services/worker-service/executor/` — Task 3's hook site; Option C lands in the compaction branch.
   - `grep -rn "conversation_log_repository\|append_entry" services/worker-service/core/` — canonical conversation-log write path (post-PR #80).
   - `grep -rn "ConversationEntryResponse\|conversation_entry" services/api-service/src/main/java/` — verify whether a stricter kind enum exists (if so, align; otherwise pass-through).
4. Read the system-prompt assembly site in `services/worker-service/executor/graph.py` — where user-supplied system prompts get merged with platform additions. Track 5 and Track 7 both appended to it.
5. Read `services/console/src/features/task-detail/ConversationPane.tsx` — the existing kind renderers. `compaction_boundary` is the closest structural precedent for the new notice.

**CRITICAL POST-WORK:**
1. Run `make worker-test`, `make api-test`, `make console-test`. All green.
2. Playwright: extend an existing scenario (or add a new one) in `docs/CONSOLE_BROWSER_TESTING.md` asserting `data-testid="conversation-entry-offload_emitted"` renders on a task with the new entry. Follow `docs/CONSOLE_TASK_CHECKLIST.md`.
3. Update `progress.md` to mark Task 5 Done.

---

## Context

Task 4 persists large tool results AND large tool-call args to S3 at ingestion, replacing them in `state["messages"]` with a URI + preview. The agent can't use that URI without a retrieval tool. This task adds:

- The agent-facing `recall_tool_result(tool_call_id)` tool.
- The system-prompt directive telling the agent when/how to call it.
- The one sanctioned mutation to `state["messages"]`: Option C reference-replacement for recalled ToolMessages that fall inside a compaction window.
- The `offload_emitted` conversation-log entry so operators see offload activity in the Console.

## Goal

When `context_management.offload_tool_results = true` (default), every agent gets a built-in recall tool, a system-prompt directive on how to use it, and Console-visible offload notices. Recalled content that later gets absorbed by compaction is replaced with a lossless reference pointing back to the original S3 object.

## Contract — Behaviour Changes

### 1. `recall_tool_result` LangChain built-in tool

- New file `services/worker-service/executor/builtin_tools/recall_tool_result.py` (create the package if missing; otherwise colocate with `memory_note` per grep).
- Tool name: `recall_tool_result` (exact string — appears in agent tool lists and logs).
- Signature (agent-visible): `recall_tool_result(tool_call_id: str) -> str`. The agent passes the `tool_call_id` of a previously-offloaded ToolMessage or AIMessage tool-call arg. Returns the full content.
- Description (agent-facing; exact wording is the implementer's choice, must convey):
  > Retrieve the full content of a previously offloaded tool output or tool-call argument. Takes the `tool_call_id` of that earlier call. The offload placeholders look like `[tool result {N} bytes @ toolresult://... preview: ...]`. Returns the original content as a string.
- Implementation: closure-bound over `(tenant_id, task_id, store)` at graph-build time — same factory pattern as `memory_note` / `memory_search` from Track 5.

### 2. Agent-visible signature and arg_key handling

Final signature: `recall_tool_result(tool_call_id: str, arg_key: str | None = None) -> str`.

- **Default `arg_key=None`** retrieves a previously offloaded tool RESULT (`ToolMessage.content`). URI reconstructed from `(tenant_id, task_id, tool_call_id)` using Task 4's result URI scheme.
- **When `arg_key` is passed** (e.g. `recall_tool_result("tooluse_abc", arg_key="content")`), the tool retrieves an offloaded tool-call ARG. The agent reads the key out of the arg placeholder it sees in context (e.g. the placeholder `"[tool arg 'content' 47KB @ toolresult://... preview: ...]"` tells the agent to pass `arg_key="content"`). URI reconstructed using Task 4's arg URI scheme.
- The content_hash component of the URI is looked up by listing the S3 prefix `tool-results/{tenant_id}/{task_id}/{tool_call_id}/` (or `.../args/{arg_key}/`) and taking the one matching hash if only one exists. If the prefix holds multiple hashes (provider retry produced multiple offloads for the same tool_call_id), return `"Error: ambiguous tool_call_id — multiple offloaded versions exist; retry with a fresher tool_call_id from a recent placeholder"`. This is rare; the common case has exactly one hash per prefix.

### 2a. Task-scope validation

- Parse the reconstructed URI via `parse_tool_result_uri`. Malformed → return `"Error: not a valid tool result id"` (do NOT fetch; store never called).
- If the URI's tenant_id or task_id don't match the current task → return `"Error: tool_call_id belongs to a different task or tenant"`. Log `recall_tool_result.cross_task_rejected` WARN. Do NOT fetch.
- (Reconstructed URIs are derived from the closure-bound `(tenant_id, task_id)` at graph-build time, so cross-task is impossible unless the agent is using pre-placeholder-format attack strings. Belt-and-suspenders: the guard still runs.)

### 3. Error-class differentiation on fetch

- `store.get(uri)` returns the content string → return the content (success).
- `store.get(uri)` returns `None` (missing key / NoSuchKey per Task 4 §1) → return `"Error: content not available (artifact may have been purged)"`. Signals expected retention GC.
- `store.get(uri)` RAISES (transport / auth / other backend failure — Task 4 propagates these) → return `"Error: artifact store temporarily unavailable; retry or continue without this content"`. Log `recall_tool_result.fetch_failed` WARN with `tenant_id`, `task_id`, `tool_call_id`, `error_type`.
- Never raise out of the tool. All paths return a string.
- Cost: S3 GET only. No LLM call, no cost-ledger row.

### 4. Special ingestion rules for `recall_tool_result`'s own output

When the ToolMessage returned by `recall_tool_result` is about to be appended to `state["messages"]`:

- It **BYPASSES** Task 4's Tier 0 ingestion offload. The agent explicitly asked for full content; re-offloading it would create a re-read loop that defeats the purpose.
- It's tagged with `additional_kwargs` so the compaction pipeline can recognise it later:

  ```
  ToolMessage(
      content=<full content>,
      tool_call_id=<new id assigned to this recall call>,
      name="recall_tool_result",
      additional_kwargs={
          "recalled": True,
          "original_tool_call_id": "<the id passed by agent>",
      },
  )
  ```

### 5. Projection rules for recalled ToolMessages

(Owned by Task 3's `pre_model_hook` — specified here for cross-reference.)

- Inside the keep window (the most recent `KEEP_TOOL_USES` tool invocations) → shown verbatim with full content.
- Outside the keep window → DROPPED from the projection. The agent can re-call `recall_tool_result` if still needed.

### 6. Option C reference-replacement — the sanctioned `state["messages"]` mutation

Implemented inside the `pre_model_hook`'s compaction branch (Task 3 builds the hook; Task 5 adds this specific behaviour to it).

- Trigger: on the compaction state update (the same update that sets `state.summary` and advances `state.summarized_through`).
- For each recalled ToolMessage (i.e. `additional_kwargs.get("recalled") is True`) whose position falls within `[previous_summarized_through, new_summarized_through)`:
  - Replace it IN PLACE with:

    ```
    ToolMessage(
        content=f"[recalled content summarized; full content remains at original "
                f"tool_call_id='{additional_kwargs['original_tool_call_id']}']",
        tool_call_id=<unchanged>,
        name=<unchanged>,
        additional_kwargs={
            **existing,
            "content_offloaded": True,
        },
    )
    ```
- This is the ONLY place in the codebase that mutates `state["messages"]`. Everything else is append-only.
- LOSSLESS: the original content remains at the ORIGINAL `tool_call_id`'s S3 key. Recovery path: the agent issues a fresh `recall_tool_result(original_tool_call_id)` and receives the content again.
- The mutation is part of the SAME state update as `state.summary` and `state.summarized_through` — no separate pass, no intermediate states observable externally.

### 7. System-prompt hint

- When `offload_tool_results = true`: append a short directive to the agent's effective system prompt (after the user-supplied prompt, before platform scaffolding). Exact wording is the implementer's choice; must convey:
  - Older tool outputs / args may appear as `[tool result N bytes @ toolresult://... preview: ...]` (or the analogous arg form).
  - To see full content, call `recall_tool_result(tool_call_id=<id>)`.
  - Fetched content counts toward the context budget on the turn it arrives.
- When `offload_tool_results = false`: no directive appended.
- Implementation: extend the system-prompt assembly site in `graph.py` identified in pre-work.

### 8. `offload_emitted` conversation-log event

- Emitted by the ingestion offload path (physically in Task 4's code — `graph.py` tool wrapper + AIMessage-append site). The convention (kind string, payload shape) is owned by Task 5.
- Emit ONCE per ingestion pass that offloaded ≥1 item (not once per item — that spams the log).
- Payload:

  ```json
  { "count": <number of items offloaded this pass>,
    "total_bytes": <sum of pre-offload byte lengths>,
    "step_index": <turn index> }
  ```
- Written via the canonical conversation-log repo (`services/worker-service/core/conversation_log_repository.py` per grep; if the file name differs post-PR #80, align with reality and do NOT invent a new module).
- Best-effort: WARN + Prometheus counter on failure, never raise.

### 9. Console rendering for `offload_emitted`

- New renderer in `services/console/src/features/task-detail/ConversationPane.tsx`. Compact inline notice — smaller than a Tier 3 boundary, not a full divider.
- Suggested copy: `"— 3 older tool outputs archived (42 KB) —"`. Exact wording at implementer's discretion.
- `data-testid="conversation-entry-offload_emitted"` so Playwright can assert presence.
- API side: verify via grep whether `ConversationEntryResponse.java` pass-through already handles unknown `kind` strings (it does as of PR #80). If a stricter enum was added since, add the entry there. Do NOT invent an enum file that doesn't exist.
- Per CLAUDE.md §Browser Verification, a new Console-visible kind REQUIRES a Playwright scenario before merge.

## Affected Files

**Before implementing: grep to confirm.**

- `services/worker-service/executor/builtin_tools/recall_tool_result.py` — **new**. If `builtin_tools/` doesn't exist, colocate with `memory_note`.
- `services/worker-service/executor/graph.py` — tool registration (gated on `offload_tool_results`), system-prompt hint assembly, special ingestion rule for the tool's own output (bypass Task 4 offload + tag `additional_kwargs`).
- `services/worker-service/executor/compaction/pipeline.py` OR `pre_model_hook.py` (whichever file Task 3 landed in) — Option C reference-replacement as part of the compaction state update.
- `services/worker-service/core/conversation_log_repository.py` (canonical per PR #80; grep to verify) — `offload_emitted` kind emission. No schema change expected.
- `services/api-service/.../response/ConversationEntryResponse.java` — verify pass-through for unknown kinds; add enum entry only if a stricter schema landed post-PR #80.
- `services/console/src/features/task-detail/ConversationPane.tsx` — new kind renderer.
- `services/console/src/features/task-detail/ConversationPane.test.tsx` — renderer unit test.
- `services/worker-service/tests/test_recall_tool_result.py` — **new**. Tool unit tests (happy path, malformed id, cross-task, store returns None, store raises, special ingestion — no re-offload of recall output, `additional_kwargs` tagging).
- `services/worker-service/tests/test_option_c_reference_replacement.py` — **new**. Tests that a recalled ToolMessage within the compaction window is replaced by the reference string with `content_offloaded=True`, and that a subsequent `recall_tool_result(original_tool_call_id)` still returns the full content from S3.
- `docs/CONSOLE_BROWSER_TESTING.md` — extend or add a scenario asserting the new `data-testid`.

## Dependencies

- **Task 4** MUST be merged first — store, URI format, `offload_tool_results` flag.
- **Task 3** MUST be merged first — the `pre_model_hook` into which Option C lands.
- Canonical landing order: 2 → 4 → 3 → **5**.

## Out of Scope for This Task

- Proactive at-ingestion offload triggers beyond Task 4's size threshold (e.g. always-offload certain tool names).
- Cross-task artifact sharing — blocked by the §2 task-scope URI validation.
- Agent UI for manually browsing artifacts — the agent drives via the recall tool; operators see via the Console's conversation pane.
- Proactive rehydration — no re-injection of recalled content on state restore; agent asks again if it needs the content again.

## Acceptance Criteria (observable behaviours)

1. Unit: `recall_tool_result(tool_call_id)` with a valid, same-task id returns the content Task 4's offload wrote. Byte-for-byte equality.
2. Unit: malformed `tool_call_id` → returns `"Error: not a valid tool result id"` without raising. Store is never queried (spy asserts zero calls).
3. Unit: cross-task `tool_call_id` (constructed for a different `task_id`) → returns the cross-task error string. `recall_tool_result.cross_task_rejected` WARN emitted. Store never queried.
4. Unit: `store.get` returns `None` → returns `"Error: content not available (artifact may have been purged)"`. Tool does not raise.
5. Unit: `store.get` RAISES (transport failure) → returns `"Error: artifact store temporarily unavailable; ..."`. Emits `recall_tool_result.fetch_failed` WARN with error type. Tool does not raise.
6. Graph test: agent with `offload_tool_results: true` has `recall_tool_result` in its tool list AND the substring `"recall_tool_result"` appears in the effective system prompt.
7. Graph test: agent with `offload_tool_results: false` has NEITHER.
8. Ingestion bypass: agent calls `recall_tool_result` on a 30KB artifact; the ToolMessage appended to `state["messages"]` has full content inline (NOT offloaded again). `store.put` is not called on this ToolMessage. `additional_kwargs["recalled"] is True` and `additional_kwargs["original_tool_call_id"]` matches the argument.
9. Option C: compaction fires on a window that contains a recalled ToolMessage → its content in `state["messages"]` becomes `"[recalled content summarized; full content remains at original tool_call_id='...']"`; `additional_kwargs["content_offloaded"] is True`; `additional_kwargs["original_tool_call_id"]` unchanged. The mutation is part of the same state update as `state.summary` / `state.summarized_through`.
10. Option C recovery: after §9's replacement, a fresh `recall_tool_result(original_tool_call_id)` returns the full original content (S3 is the durable source).
11. Projection: recalled ToolMessage inside the keep window → shown verbatim in the `pre_model_hook`'s output. Outside the keep window → absent from that output.
12. Conversation log: a pipeline pass that offloads 3 items → exactly ONE `offload_emitted` entry with `count=3` and `total_bytes` equal to the summed pre-offload byte lengths.
13. Console unit test: a task with one `offload_emitted` entry renders the inline notice. `data-testid="conversation-entry-offload_emitted"` present.
14. Playwright scenario: on live stack, the new entry renders on a task that offloaded content. Scenario added to `CONSOLE_BROWSER_TESTING.md` per CLAUDE.md §Browser Verification.
15. Cross-tenant safety: a `tool_call_id` whose reconstructed URI points to a different tenant returns the cross-task error; no S3 fetch attempted.

## Pattern references in existing code

- Built-in tool with closure-bound tenant/task/store: `memory_note` / `memory_search` (Track 5) — follow this factory pattern.
- System-prompt assembly site: same site Track 5 and Track 7 extended when merging platform additions into the agent's system prompt.
- Conversation-log event emission from compaction: Track 7 Task 13's `compaction_boundary` emission — use as a template for `offload_emitted` (same repo, same best-effort + WARN-on-failure contract).
- Console kind renderer: the 9 existing renderers in `ConversationPane.tsx`; `compaction_boundary` is structurally closest (inline informational element, not a full divider).
- The only prior sanctioned mutation to message state: none — Option C is net-new. Be explicit in comments that this is the one exception to the append-only rule so future readers don't over-generalise.

<!-- AGENT_TASK_END -->
