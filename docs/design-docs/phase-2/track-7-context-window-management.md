# Track 7 Design — Context Window Management

**Status: Design complete. Implementation plan next.**

## Why this track exists

Long-running tasks accumulate tool-call arguments and tool-result content in the message history that LangGraph replays to the LLM on every step. Input tokens grow monotonically with the number of tool invocations. In one observed production task, input reached ~27,000 tokens with ~84,000 characters of tool content in history. The main offenders were `sandbox_write_file` arguments and `sandbox_read_file` results embedded in past `AIMessage` / `ToolMessage` records.

Without a compaction layer, tasks with more than a small number of tool invocations hit one of three failure modes:

1. provider context-window limits
2. rate-limit / TPM ceilings
3. cost-per-call that makes the task economically non-viable

This track closes that gap by adding a tiered compaction layer that runs inside the LangGraph executor loop, transforming the message list just before each LLM call. No schema changes, no new services, no new APIs — this is a worker-local transform.

## Relationship to other tracks

**Track 5 (Memory).** Memory is a cross-task store; Track 7 is a within-task transform. They share zero schema, zero API, and zero UI. They do not block each other. If Track 7 ships first, the final memory-write node in Track 5 operates on a compacted history, which is cheaper to summarize. If Track 5 ships first, memory already works correctly — long tasks are simply more likely to fail before reaching the memory-write node. Track 7 is recommended ahead of Track 5 on the grounds that memory is less useful if long-running tasks cannot complete.

**Track 7 calls into Track 5 at one specific point:** when Tier 3 summarization is about to fire for the first time in a task, Track 7 inserts a short agentic turn instructing the agent to call `memory_note` for anything worth persisting before older message detail is lost. This is the only Track 5 ↔ Track 7 coupling.

**Track 8 (Coding-Agent Primitives, proposed).** Track 8 is the *primary* defense against large tool results — agent-controlled chunking via `sandbox_read_file(offset, limit)`, `sandbox_exec` output truncation, `sandbox_grep` `head_limit`. Track 7 is the *secondary* defense — assumes tools may misbehave (BYOT from Track 4, legacy tools before Track 8 lands) and caps per-result size at ingestion regardless. The two tracks are complementary: Track 8 prevents problems at the source; Track 7 handles whatever still reaches history. Track 7 does not block on Track 8.

**Track 4 (BYOT).** Customer-provided MCP servers can return arbitrary-size tool results. Track 7's per-result cap is the guardrail that keeps a misbehaving custom tool from destroying an agent's context.

## Scope

**In scope:**

- Tier 1 — tool-result clearing (observation masking): replace older `ToolMessage` content with deterministic placeholders.
- Tier 1.5 — tool-call argument truncation: truncate large `tool_calls` arguments in older `AIMessage` records.
- Tier 3 — retrospective LLM summarization: single summary span replacing the oldest message prefix, fired only when Tier 1 + 1.5 together cannot get input below the tier-3 threshold.
- Per-tool-result cap at ingestion (wraps every `ToolMessage` as it enters history).
- Agent config extension: `agent_config.context_management` with platform-owned defaults and narrow per-agent overrides.
- Pre-Tier-3 memory flush when `agent.memory.enabled=true` — one agentic turn to call `memory_note` before the first Tier 3 firing.
- Monotone watermark state fields on the LangGraph graph state (cache-stability invariant).
- Observability: structured logs + Langfuse spans + per-agent metrics.
- Console agent-config UI surface for the narrow override set.

**Out of scope for v1 (deferred):**

- Use of Anthropic's native API primitives (`clear_tool_uses_20250919`, `compact_20260112`, `memory_20250818`). v1 does client-side compaction only for portability across providers (OpenAI, Gemini, BYOT). Track 7 follow-up can layer server-side primitives on Anthropic-provider agents once v1 is stable.
- Per-task compaction overrides on `POST /v1/tasks`. v1 keeps configuration at the agent level; per-task knobs add surface area without a demonstrated use case.
- Learned / RL-trained compaction policies (MEM1, ACON). Implementing now would conflate an infrastructure delivery with a research agenda.
- Smart semantic clearing (e.g., "keep tool results that the agent referenced in later reasoning"). The Anthropic cookbook and JetBrains empirical work both show position-based masking wins; revisit only if metrics justify it.
- Cross-task compaction history (e.g., "agent X's Tier 3 summaries"). Out of scope — that is memory (Track 5).
- Compaction of the Agent's system prompt or the platform system message. These are stable and small; compacting them would invalidate KV-cache prefixes on every task resume.

## Core design rules

Every downstream decision follows from these four rules.

### 1. KV-cache preservation is the dominant cost lever

Cached input is up to **10× cheaper** than uncached input on the providers we support (Anthropic cached $0.30 / MTok vs uncached $3.00 / MTok on Sonnet; similar ratios on OpenAI). For a managed platform, the compaction layer's effect on cache-hit rate is a larger cost driver than the raw token count it saves.

Rewriting the history prefix on every LLM call destroys the cache. The compaction transform MUST be **deterministic and monotone**: once a message at position `i` is rewritten to some placeholder `p`, every subsequent LLM call within the task must see the same placeholder `p` at position `i`. Never un-rewrite, never re-rewrite with a different string.

This is enforced by monotone watermark state fields (`cleared_through_turn_index`, `truncated_args_through_turn_index`, `summarized_through_turn_index`). Watermarks only advance; they never retract.

### 2. Observation masking > LLM summarization

JetBrains empirical result (NeurIPS DL4C 2025, SWE-bench Verified, 500 instances, up to 250 turns): observation masking beat LLM summarization on both cost and task solve rate in 4 of 5 settings, and summarization produced +15% trajectory elongation because summaries smooth over signals the agent needs to decide to stop.

Therefore Tier 1 (masking) and Tier 1.5 (argument truncation) run on every call. Tier 3 (summarization) is a last-resort fallback, not the main mechanism.

### 3. Silent compaction — don't induce "context anxiety"

Cognition's published experience with Devin: agents change behavior when they notice they are approaching the context limit ("context anxiety"). They start cutting corners and premature-summarizing on their own. Cognition's fix was to make compaction invisible to the agent.

Track 7 follows the same rule. Tool-result placeholders use neutral language ("tool output not retained"); no `SystemMessage` is injected announcing that compaction ran; the Tier 3 summary marker looks like a normal assistant-side state summary, not a meta-instruction.

**Exception:** the pre-Tier-3 memory flush is a one-shot nudge *before* summarization to give the agent a chance to preserve salient context via `memory_note`. This is opt-in per agent via `memory.enabled` AND `context_management.pre_tier3_memory_flush`, and it fires at most once per task.

### 4. Platform-owned defaults with narrow per-agent tuning knobs

Per the "managed runtime, not a personal agent" positioning: customers do not need to tune knobs to get a working agent. Platform defaults (25KB per-result cap, keep=3 tool uses, 50% / 75% trigger fractions) apply to every agent. The per-agent tuning surface is **narrow on purpose** — `summarizer_model`, `exclude_tools`, `pre_tier3_memory_flush`. None of these are feature toggles; they're tuning for agents with unusual tool surfaces. Thresholds (`trigger_fraction_*`, `keep`, `per_result_cap_bytes`) are platform-owned and are promoted to per-agent only if production metrics show a clear use case. Context management itself is not toggleable per agent — see §Scope for why.

## Architecture overview

```
┌────────────────────────────────────────────────────────────────────┐
│ Tool execution (existing)                                          │
│   result = await tool.ainvoke(args)                                │
│   ── per-tool-result cap ──► ToolMessage.content capped to 25KB    │
│   return ToolMessage(content=capped, tool_call_id=...)             │
└────────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│ agent_node (modified)                                              │
│   raw_messages = state["messages"]                                 │
│                                                                    │
│   compacted, watermarks, events = compaction.compact_for_llm(      │
│       raw_messages,                                                │
│       state.cleared_through_turn_index,                            │
│       state.truncated_args_through_turn_index,                     │
│       state.summarized_through_turn_index,                         │
│       state.summary_marker,                                        │
│       config=agent_config.context_management,                      │
│       model_context_window=N,                                      │
│   )                                                                │
│                                                                    │
│   # Tier 1: clear old ToolMessage.content                          │
│   # Tier 1.5: truncate old AIMessage.tool_calls[*].args            │
│   # Tier 3 (if still over threshold):                              │
│   #   - fire pre-Tier-3 memory flush (if enabled, once per task)   │
│   #   - call summarizer LLM, write summary_marker                  │
│                                                                    │
│   response = await llm_with_tools.ainvoke(compacted, config)       │
│   return {                                                         │
│       "messages": [response],                                      │
│       **watermarks,   # monotone advances only                     │
│   }                                                                │
└────────────────────────────────────────────────────────────────────┘
```

The raw `messages` on graph state are **never** mutated in place. Tier 1/1.5 produce an in-memory compacted view for the LLM call; the checkpointer persists the raw history (plus watermarks and the optional `summary_marker`). Tier 3 is the one exception — it writes `summary_marker` to state and advances `summarized_through_turn_index`, so subsequent calls skip the summarized prefix entirely.

## Agent config extension

New sub-object on `agent_config`:

```json
{
  "context_management": {
    "summarizer_model": "claude-haiku-4-5",
    "exclude_tools": ["web_search", "custom_tool_x"],
    "pre_tier3_memory_flush": true
  }
}
```

**Fields:**

| Field | Type | Default | Override scope |
|-------|------|---------|----------------|
| `summarizer_model` | string (model id) | platform default (`claude-haiku-4-5`) | per-agent |
| `exclude_tools` | list\[string\] | `[]` | per-agent additive to platform allowlist |
| `pre_tier3_memory_flush` | bool | `true` | per-agent, no-ops when `memory.enabled=false` |

**No per-agent opt-out.** Context management is platform infrastructure, not a feature. An agent that hits the provider's context-window ceiling without compaction simply fails — there is no graceful "verbatim history" mode, because the per-tool-result cap already makes "verbatim history" impossible the moment compaction exists at all. Every agent gets all three tiers + the per-result cap.

**`exclude_tools`:** tools whose *results* must never be masked. Use case: `request_human_input`'s response is load-bearing even after dozens of follow-up turns; if it is masked to `[tool output not retained]` the agent forgets what the human said. Platform seeds the list with:

- `memory_note`, `save_memory` — small, durable agent statements the agent may reference indefinitely.
- `request_human_input` — the human response is the pivot for the entire task; masking it erases the reason the agent paused.
- `memory_search`, `task_history_get` — the agent explicitly retrieved these to inform the current task. Masking what the agent *just chose to fetch* defeats the fetch.

Any other small, semantically-dense tool should also go here; agents can extend via `context_management.exclude_tools`.

**Platform-owned (not per-agent) in v1:**

| Constant | Value | Source |
|----------|-------|--------|
| `TIER_1_TRIGGER_FRACTION` | 0.50 of effective budget | fraction of model context that triggers masking |
| `TIER_3_TRIGGER_FRACTION` | 0.75 of effective budget | fraction of model context that triggers summarization |
| `OUTPUT_BUDGET_RESERVE_TOKENS` | 10_000 | subtracted from model context when computing effective budget (leaves room for the model's response) |
| `MIN_TIER_SEPARATION_TOKENS` | 2_000 | small-context guardrail: ensures Tier 3 fires strictly above Tier 1 |
| `KEEP_TOOL_USES` | 3 | most recent tool-use turns kept intact (tier 1 protection window) |
| `PER_TOOL_RESULT_CAP_BYTES` | 25_000 | hard cap at ToolMessage ingestion (byte-count, not token-count) |
| `TRUNCATABLE_TOOL_ARG_KEYS` | `content`, `new_string`, `old_string`, `text`, `body` | arg keys that get truncated at Tier 1.5 |
| `ARG_TRUNCATION_CAP_BYTES` | 1_000 | an arg longer than this, in an older turn, is replaced with `[<n> bytes]` |
| `SUMMARIZER_MAX_RETRIES` | 2 | after exhaustion, Tier 3 is skipped for this call and re-attempted on the next call |

These live as module-level constants in the worker (`services/worker-service/executor/compaction/defaults.py`) with inline citations of the Anthropic cookbook and JetBrains paper. They are **not** exposed via API. Promoting any of them to per-agent is a deliberate future change that requires production telemetry justifying the knob.

**Per-model threshold resolution** (`compaction/thresholds.py`):

```
def resolve_thresholds(model_context_window: int) -> Thresholds:
    effective_budget = model_context_window - OUTPUT_BUDGET_RESERVE_TOKENS
    tier1_trigger = int(effective_budget * TIER_1_TRIGGER_FRACTION)
    tier3_trigger = int(effective_budget * TIER_3_TRIGGER_FRACTION)
    # Guardrail: Tier 3 must always be strictly greater than Tier 1. On
    # pathologically small models (e.g., 8K context), the fraction terms
    # can collapse close together; bump Tier 3 by a minimum headroom so
    # the two tiers don't fire simultaneously.
    if tier3_trigger - tier1_trigger < MIN_TIER_SEPARATION_TOKENS:
        tier3_trigger = tier1_trigger + MIN_TIER_SEPARATION_TOKENS
    return Thresholds(tier1=tier1_trigger, tier3=tier3_trigger)
```

Thresholds are a pure function of the model's context window. No absolute token cap — customers who picked a 1M-context model want the room, and the platform should not second-guess that choice by clearing at 10% of the window.

| Model context | Effective budget | Tier 1 trigger (50%) | Tier 3 trigger (75%) |
|---------------|------------------|----------------------|----------------------|
| 8K            | ~-2K → 0K        | ~0K (Tier 3 +2K min) | 2K (min separation)  |
| 128K (GPT-4o) | ~118K            | ~59K                 | ~88K                 |
| 200K (Sonnet) | ~190K            | ~95K                 | ~142K                |
| 1M (Gemini)   | ~990K            | ~495K                | ~742K                |

**Cost-sensitive customers can opt into tighter compaction** in a future Track 7 follow-up via `context_management.aggressive_compaction=true`, which halves both fractions. Deliberately deferred from v1 — no production telemetry yet shows a threshold tightening is needed, and a half-fraction knob is cleaner than any platform-wide absolute cap.

**Tokens vs bytes.** Thresholds are measured in **tokens**. Estimation strategy:

- **Anthropic and OpenAI:** use the provider's real tokenizer (`anthropic.count_tokens`, `tiktoken`). Cheap, local, avoids the "real tokens are 30% more than our heuristic" failure mode on code/JSON-heavy history.
- **Gemini / other providers / BYOT:** fall back to `len(serialized_text) / 3` character-count heuristic. Documented as ±30% accuracy; if a Gemini task is persistently firing Tier 3 late, we add Google's tokenizer.

Heuristic-only implementations are forbidden for Anthropic and OpenAI — the code/JSON-heavy agent histories Track 7 exists to compact are exactly where the heuristic is least accurate (real tokens can be 30–50% higher than `len/3.5` for dense JSON). Using the real tokenizer is the difference between Tier 3 firing with a reasonable margin vs firing past the model's hard context limit.

The per-tool-result cap is measured in **bytes** because it runs at tool-execution time, before any tokenization. Byte-level cap is cache-stable (tokenizer-agnostic) and matches what every HTTP client / tool runtime already has.

**Model context window source.** Read from the `models` table row for the agent's primary model at graph-build time; cache for the lifetime of the `execute_task` invocation. For BYOT / custom models not in `models`, default to **32K tokens** (conservative) and emit a structured `compaction.model_context_window_unknown` warning log at graph build so operators can notice. Never guess upward — a 32K fallback for a model that actually has 200K just means we compact earlier than optimal; a 200K fallback for a model that actually has 32K causes hard-floor dead-letters.

**Recomputed per LLM call, not cached.** `resolve_thresholds` is pure and called once per agent-node call. No state required.

**Validation rules** (mirror Track 5's pattern):

- `summarizer_model` must reference an active row in `models` for the agent's provider; otherwise 400 at `POST/PUT /v1/agents`.
- `exclude_tools` must not exceed 50 entries (matches `tool_servers` cap).
- `pre_tier3_memory_flush` bool-typed; no coercion.
- Canonicalization preserves the sub-object verbatim; absence remains absence (no silent defaults written).

## Per-tool-result cap at ingestion

Every tool result entering history is hard-capped to `PER_TOOL_RESULT_CAP_BYTES` (25KB) **before** it becomes a `ToolMessage` in state. The cap lives in the tool-execution wrapper, not in compaction itself.

**Algorithm:**

```
def cap_tool_result(raw: str, tool_name: str) -> tuple[str, CapEvent | None]:
    if len(raw.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES:
        return raw, None
    head = raw[:PER_TOOL_RESULT_CAP_BYTES // 2]
    tail = raw[-(PER_TOOL_RESULT_CAP_BYTES // 2):]
    capped = (
        f"{head}\n"
        f"[... truncated {len(raw) - PER_TOOL_RESULT_CAP_BYTES} bytes. "
        f"Tool returned {len(raw)} bytes total; use a narrower query or "
        f"smaller offset/limit to read the rest. ...]\n"
        f"{tail}"
    )
    return capped, CapEvent(tool=tool_name, orig_bytes=len(raw), capped_bytes=len(capped))
```

**Head + tail split** (not head-only) preserves both the start of output (command echoes, headers) and the end (final result, error message). This mirrors Claude Code's exec truncation.

**Why at ingestion, not in compaction:**

- The compaction layer (Tier 1/1.5/3) only touches *older* messages behind the protection window. A pathological single tool result (e.g., 500KB JSON blob) falls inside the protection window and would still blow the context. The cap at ingestion prevents that.
- It is cache-stable: the capped string is what gets written to state, checkpointed, and replayed. No subsequent transform changes it.
- It composes with Track 8's `sandbox_exec` `max_output_bytes` (Track 8 is tighter / semantic; Track 7's cap is the backstop).

**Applies to every ToolMessage**, including:

- Built-in tools (`sandbox_*`, `web_search`, etc.)
- Track 4 BYOT MCP tool results
- Memory tools (`memory_search`, `task_history_get`) — though their outputs are normally small
- Human input (`request_human_input`) — typically small; capped same as everything else

**Track 8 coordination.** Track 8 (proposed) adds tool-specific per-call caps (`sandbox_exec.max_output_bytes`, `sandbox_grep.head_limit`). Those are *semantic* caps — the tool decides how to truncate (e.g., keep all regex match lines, drop overflow). They must be **strictly less than or equal to** Track 7's 25KB ceiling; if a Track 8 tool returns more than 25KB it will be head+tail re-capped, destroying the semantic slicing. Task specs for Track 8 tools must assert their per-tool caps satisfy `tool_cap_bytes ≤ PER_TOOL_RESULT_CAP_BYTES`.

**When the cap fires, emit** `compaction.per_result_capped` structured log + Langfuse span annotation with `{tool, orig_bytes, capped_bytes}`.

## Tier 1: tool-result clearing (observation masking)

**When:** every LLM call where estimated input > `resolve_thresholds(model_context_window).tier1`. Below the threshold, no masking — raw history is cheaper to serve to the LLM and preserves cache. The threshold is a pure fraction (50%) of the model's effective budget, so a 200K Sonnet agent masks above ~95K and a 1M Gemini agent masks above ~495K.

**Algorithm:**

```
tool_use_positions = [i for i, m in enumerate(messages) if is_tool_message(m)]
# Most recent KEEP_TOOL_USES stay intact. Everything older is candidate.
protect_from_index = tool_use_positions[-KEEP_TOOL_USES] if len(tool_use_positions) > KEEP_TOOL_USES else 0
new_cleared_through = max(state.cleared_through_turn_index, protect_from_index)

compacted = list(messages)
for i, m in enumerate(messages):
    if i >= new_cleared_through:
        continue
    if not is_tool_message(m):
        continue
    tool_name = m.name or tool_name_from_call_id(messages, m.tool_call_id)
    if tool_name in exclude_tools_effective:
        continue
    if m.content_already_cleared_marker:  # deterministic: already cleared, leave as-is
        continue
    compacted[i] = replace_with_placeholder(m)

return compacted, {"cleared_through_turn_index": new_cleared_through}
```

**Placeholder shape:** `[tool output not retained — {tool_name} returned {orig_bytes} bytes at step {step_index}]`. Neutral language per the "silent compaction" rule. The `tool_name` and `step_index` give the agent enough anchor to call the tool again if it needs the data.

**Monotonicity guarantee:** `cleared_through_turn_index` only advances. A message cleared at call N stays cleared at all calls ≥ N. This keeps the KV-cache prefix stable for each call boundary that has already been served.

**Exclude list:** `exclude_tools_effective = PLATFORM_EXCLUDE + agent.exclude_tools`. Platform default: `[memory_note, save_memory, request_human_input]` — these tools' outputs are load-bearing across many turns.

**No-op case:** if `cleared_through_turn_index` already points past all candidate older tool messages, Tier 1 produces `compacted == messages`. No cost, no state change.

## Tier 1.5: tool-call argument truncation

**When:** same trigger as Tier 1 (runs in the same pre-LLM pass).

**Algorithm:**

```
for i, m in enumerate(messages):
    if i >= new_truncated_through:
        continue
    if not is_ai_message(m) or not m.tool_calls:
        continue
    for call in m.tool_calls:
        for key, val in list(call.args.items()):
            if key not in TRUNCATABLE_TOOL_ARG_KEYS:
                continue
            if not isinstance(val, str):
                continue
            if len(val) <= ARG_TRUNCATION_CAP_BYTES:
                continue
            call.args[key] = f"[{len(val)} bytes — arg truncated after step {i}]"

return compacted, {"truncated_args_through_turn_index": new_truncated_through}
```

Targets the observed worst offender in production: `sandbox_write_file.content` arg, which holds the entire file contents the agent wrote. After the file is written, that arg is pure token waste — the agent never needs to re-read its own input.

**Truncatable arg keys** live in the platform constants. Adding a new key (e.g., `patch` once Track 8's `sandbox_edit` ships) requires a one-line update, not a per-agent config change.

**Non-string args are untouched.** We only target large string payloads; structured args (numbers, booleans, short strings, dicts, lists) stay.

**Cache monotonicity** identical to Tier 1 — truncation is a deterministic function of `(message_index, tool_call_index, key, original_value)`.

## Tier 3: retrospective LLM summarization

**When:** Tier 1 + 1.5 have already run and estimated input is still > `resolve_thresholds(model_context_window).tier3`. The threshold is a pure fraction (75%) of the model's effective budget: ~142K on a 200K Sonnet agent, ~742K on a 1M Gemini agent, with a minimum-separation guardrail on tiny-context models.

**Algorithm:**

```
# summarize everything before the protection window
protect_from = tool_use_positions[-KEEP_TOOL_USES]
to_summarize = messages[state.summarized_through_turn_index : protect_from]
if len(to_summarize) < 2:
    return compacted, events  # nothing to summarize yet

# one-shot, cheap-model
summary = await summarizer_llm.ainvoke([
    SystemMessage(SUMMARIZER_PROMPT),
    HumanMessage(format_messages_for_summary(to_summarize)),
])

# write monotone state
new_summary_marker = (
    (state.summary_marker or "")
    + "\n\n"
    + f"[Earlier context summary through step {protect_from}]:\n"
    + summary.content
)
new_summarized_through = protect_from

compacted = [
    SystemMessage(content=new_summary_marker, additional_kwargs={"compaction": True}),
    *messages[protect_from:],
]
```

**Summarizer prompt structure** (lives in `compaction/summarizer.py`, citable by future readers):

```
You are compressing a portion of an autonomous agent's tool-use history so the
agent can continue the task within its context window. Produce a compact
factual summary (≤ 400 words) that preserves:
- Files the agent has created, read, or modified (full paths)
- External URLs or API responses whose contents matter for the rest of the task
- Decisions the agent has committed to and their reasoning
- Errors encountered and whether they were resolved
- Parameters or identifiers the agent will need later (IDs, keys, names)

Do NOT:
- Address the agent in the second person.
- Invent next steps or give instructions.
- Comment on the compression itself.
Return the summary only.
```

**Monotonicity:** `summary_marker` is append-only across compactions within a task. `summarized_through_turn_index` only advances. If the task reaches the Tier 3 trigger a second time (more history has accumulated), a new summary is generated for the newly-old slice and **appended** to the existing marker — we do not re-summarize the prefix that is already summarized. This preserves the KV-cache on the summary itself.

**Cost ledger:** every Tier 3 call writes one row to `agent_cost_ledger` attributed to the task's current checkpoint, tagged `operation='compaction.tier3'`. Uses the provider for `summarizer_model`; pricing from the `models` table.

**Budget interaction (Track 3):** Tier 3 is named in the same budget carve-out as Track 5's `memory_write` — per-task pause check skipped for `compaction.tier3` specifically. Hourly-spend accounting still applies. Rationale: Tier 3 runs when the task is *already committed* to an expensive path; pausing mid-compaction leaves the task unable to continue at all.

**Summarizer outage:** after `SUMMARIZER_MAX_RETRIES` exhausted, Tier 3 is skipped for *this* call. No summary marker is written; `summarized_through_turn_index` is not advanced. The next agent-node call will re-attempt if the threshold still fires. If Tier 1/1.5 together can keep input under the model's hard limit, the task continues; otherwise the LLM call itself errors and the task retries / dead-letters via the existing path. The task does not implicitly escalate to the `context_exceeded_irrecoverable` dead-letter just because summarizer is flaky — that dead-letter is reserved for the hard-floor case below.

**Deterministic replay is best-effort.** Different summarizer calls with the same input can return different text. LangGraph checkpointing persists the summary marker, so on worker crash/resume *after* the summarizer LLM call returned, the stored marker is authoritative. On crash *during* the summarizer call, the next attempt will produce a possibly-different marker — this is acceptable because the summary is the *only* state field that breaks strict determinism, and it is not replayed to the agent as a decision-making input.

## Pre-Tier-3 memory flush

Gated on: `agent.memory.enabled AND context_management.pre_tier3_memory_flush AND NOT state.memory_flush_fired_this_task`.

When all three hold, and Tier 3 would otherwise fire this call, the compaction pipeline first inserts a one-shot agentic turn:

```
SystemMessage(
  "You are about to have older context summarized. This is your one chance "
  "in this task to preserve cross-task-valuable facts. Call memory_note for "
  "anything you want to remember in future tasks — decisions, user "
  "preferences, non-obvious facts. If nothing qualifies, reply with an "
  "empty response."
)
```

**Control flow:**

1. Pipeline detects Tier 3 would fire AND pre-flush conditions hold.
2. Pipeline sets `state.memory_flush_fired_this_task = True` (monotone — never reset within a task).
3. Pipeline returns compacted messages with the pre-flush system message appended. The main agent LLM call proceeds normally on this turn.
4. If the agent calls `memory_note`, the `memory_note` tool result (small) lands on the protection window; the agent sees it on the next call.
5. On the *next* agent-node call, the pre-flush flag is already set, so Tier 3 now fires (if the threshold is still exceeded).

**Fires at most once per task.** Runbooks matter less than stability — a second flush risks the agent flooding memory with duplicates.

**Skipped on heartbeat / recovery turns.** If the current agent-node call is entered without any new `ToolMessage` or `HumanMessage` having been appended since the last agent super-step, the flush is skipped. The detection rule is **positional, not message-pair-based**: compare the current message list length (or a persisted `last_flush_check_position` watermark on state) against the state snapshot at the end of the previous agent super-step. If nothing new was added, it's a heartbeat/recovery turn.

This is the same rule used in §Validation #8, stated once here and once there. The "last two messages are both `AIMessage`" heuristic is NOT correct — it misfires on rate-limit retry loops (the previous call produced an AIMessage but the retry is a legitimate new super-step) and on pure-reasoning turns (two consecutive AIMessages can be valid).

Implementation: track `last_super_step_message_count` as part of `RuntimeState` (added in Task 8 alongside the Track 7 watermark fields). Before each compaction pass, compare `len(messages) > last_super_step_message_count` — true means new input arrived and the flush is eligible; false means heartbeat and the flush is skipped. Watermark is `max`-reduced.

**Follow-up / redrive interaction.** The `memory_flush_fired_this_task` flag lives in graph state, which is checkpointed and survives crash/resume. A follow-up or redrive that resumes from a pre-flush checkpoint continues to treat the flush as unfired; a follow-up / redrive that resumes after the flush already fired does not re-fire it.

**Opt-out paths:**

- `agent.memory.enabled = false` → flush never fires (no memory system to note into)
- `context_management.pre_tier3_memory_flush = false` → flush never fires on this agent regardless of memory state

**Edge case: flush turn pushes input past the model's hard context limit.** The flush inserts one SystemMessage and skips Tier 3 *for this call only*. If the Tier 1 + 1.5 output plus the flush prompt exceeds the model's hard context window (rare — requires history to be within ~200 bytes of the ceiling before the flush adds its ~500-byte message), the LLM call will error with provider-side context-limit. This surfaces through the worker's existing rate-limit / retryable-error path — the agent-node retry re-enters `compact_for_llm`, the `memory_flush_fired_this_task` flag is now True (already written by the flush turn), so Tier 3 proceeds normally on the retry. The flush is not re-inserted. No special handling is needed; the existing retry path covers it.

**SystemMessage injection shape.** The flush message is appended as a new `SystemMessage` at the END of the compacted list. This is an in-memory-only mutation for the single LLM call — the flush message is **NOT** returned in `state_updates["messages"]`, so it is not persisted to checkpoint history. Only the `memory_flush_fired_this_task=True` flag is persisted. The next agent-node call rebuilds the compacted view without the flush message.

**Provider compatibility note.** Some providers constrain SystemMessage placement (OpenAI requires exactly one at index 0; Gemini shims system messages through LangChain). The pipeline handles this by always *prepending to or appending a new line within* the existing first SystemMessage when a second SystemMessage would be disallowed. The detection for this is per-provider and lives in the pipeline alongside existing provider-specific message shaping.

## Hard floor and dead-letter path

Minimum viable LLM input: most recent `ToolMessage` + most recent `AIMessage` + system prompts + the current turn's input. These are never truncated.

If, after Tier 1 + 1.5 + 3 + protection-window shrink, the estimated input still exceeds the model's context window, the task transitions to dead-letter with reason `context_exceeded_irrecoverable`. This is a signaling event, not an expected code path — with `PER_TOOL_RESULT_CAP_BYTES=25000` and a 200K-context model, the hard floor is > 50 tool uses of capped content, which would require the agent's protection window itself to exceed 150K. In practice, this path catches either:

- A misconfigured model with an unusually small context window (say, 8K).
- A severely misbehaving summarizer (e.g., the summary marker itself exceeds 150K — never observed in testing).

**Migration shape.** `dead_letter_reason` is a `TEXT` column with a `CHECK` constraint (see `infrastructure/database/migrations/0010_sandbox_support.sql`), not a Postgres enum. Adding a new reason requires `ALTER TABLE tasks DROP CONSTRAINT tasks_dead_letter_reason_check` + re-add with the expanded allowed-values list. Task 9's migration uses the pattern in `0010_sandbox_support.sql` verbatim. **Deploy order is a hard constraint** — the migration must land *before* worker code that can produce the new reason, or the `UPDATE tasks` transition will fail the CHECK and the reaper will be unable to dead-letter a stuck task.

## State schema extensions

**One unified `RuntimeState` schema, feature-independent.** Every task on the worker uses the same TypedDict regardless of which features are enabled for its agent. Features branch by *graph topology* (which nodes and edges get wired) and *runtime pipeline checks* (which transforms run), **not** by schema swapping. This is the idiomatic LangGraph pattern (langgraph-swarm-py, open_deep_research, chat-langchain all do this) and the only one the framework's persistence layer supports — LangGraph explicitly provides no schema-migration API ([langgraphjs #536](https://github.com/langchain-ai/langgraphjs/issues/536)), so per-task schema swapping puts redrive / follow-up on the unsupported side of the checkpointer.

```python
class RuntimeState(TypedDict):
    # Core (always populated)
    messages: Annotated[list[BaseMessage], add_messages]

    # Track 5 (Memory) fields — populated by memory-enabled graphs; untouched
    # otherwise. Note: default is `[]`, not None. LangGraph's operator.add
    # reducer crashes on None; sentinels must be reducer-safe.
    observations: Annotated[list[str], operator.add]    # default []
    pending_memory: dict                                # default {}
    memory_opt_in: bool                                 # default False

    # Track 7 (Context Management) fields — populated by the compaction
    # pipeline on every task.
    cleared_through_turn_index: Annotated[int, _max_reducer]             # default 0
    truncated_args_through_turn_index: Annotated[int, _max_reducer]      # default 0
    summarized_through_turn_index: Annotated[int, _max_reducer]          # default 0
    summary_marker: Annotated[str, _summary_marker_strict_append]        # default ""
    memory_flush_fired_this_task: Annotated[bool, _any_reducer]          # default False
    last_super_step_message_count: Annotated[int, _max_reducer]          # default 0
```

**Field-typing discipline derived from LangGraph research:**

- **No `Optional[T]` (or any non-instantiable type) on reducer-backed fields.** When `typ()` is non-instantiable (unions, `Optional`, BaseModel with required fields), LangGraph's `BinaryOperatorAggregate` leaves the channel `MISSING` and the *first* node write becomes the seed without running the reducer. Strict-append and `max` invariants cannot hold on that first write. Use direct types + reducer-safe sentinel defaults (`""` for strings, `{}` for dicts, `[]` for lists, `0` for counters) so `typ()` always yields a usable initial value. See `langgraph/channels/binop.py` lines 65-68 + 105-107 and the closed-as-by-design [issue #4305](https://github.com/langchain-ai/langgraph/issues/4305).
- **Reducer-safe defaults.** `operator.add` on `None` raises; initial state is constructed with empty-but-typed values so no call-site hits a bare `None`.
- **Append-only schema discipline.** Adding fields is safe (old checkpoints deserialize with the new fields missing, TypedDict tolerates). Removing or renaming fields is not safe — the checkpointer has no migration story, so schema evolution is strictly append-only. A regression test (load a V1 checkpoint fixture against the current schema, assert it deserialises cleanly) guards this.
- **Subgraph composition as the future escape hatch.** If state outgrows ~15 fields or features become truly orthogonal, we carve the biggest feature into a subgraph with its own schema and transform at the boundary. That's the framework-sanctioned split; we don't need it yet.

**Reducers:** watermark fields use a `max` reducer (not stock `operator.add`), expressing monotonicity at the schema level. A stale LangGraph super-step that returns `{cleared_through_turn_index: 5}` when the current value is `10` is ignored by the reducer rather than silently regressing the watermark.

The `summary_marker` uses a **strict-append reducer**: the new value MUST start with the old value (prefix check). Any other input raises a structured error (`compaction.summary_marker_non_append`) and the reducer returns the *old* value — the non-append write is rejected. This is a hard invariant. Rationale: any non-append rewrite changes byte 0 of the compacted prefix, which invalidates the KV-cache for every subsequent LLM call in the task.

`memory_flush_fired_this_task` uses an `or` reducer (monotone one-shot).

Rollback (Phase 2 Track 2's `rollback_last_checkpoint`) is handled outside the reducer: the checkpointer restores a prior state snapshot wholesale. Rollback is the legitimate way to "move backward" on the marker; reducers never execute that path.

**Why unified-state over feature-switched schemas:**

1. **Checkpoint shape is stable.** A task checkpointed today and resumed next month after the agent's config has been edited (memory flipped on/off, exclude_tools changed) deserialises cleanly into the same schema. No silent field drops.
2. **One test matrix.** Every state test covers one TypedDict. No N×M combinatorics across enabled/disabled feature pairs.
3. **Future features add fields, not branches.** Track 9 (planning primitive), Track 10 (deep research), etc., each extend `RuntimeState` additively. No per-feature schema class.
4. **Cost is negligible.** LangGraph reducers on unused fields are O(1) calls with empty updates; the checkpointer serializes `None` / `[]` / `0` in a few bytes.

**Features still branch on topology**, not state:

- Track 5 `memory_write` node is registered iff `agent.memory.enabled`. Memory-disabled agents have no `memory_write` node; the `agent → END` edge is used.
- Track 7 compaction pipeline runs inside `agent_node` for every task; there is no per-worker or per-agent toggle.
- Track 5 memory tools (`memory_note`, `memory_search`, `task_history_get`) are registered on the LLM iff memory is enabled — same as today.

This is a migration of the pattern Track 5 currently uses (`MemoryEnabledState if stack_enabled else MessagesState`). The plan rolls that migration into Task 7 so both tracks end up consistent.

## Checkpoint interaction

**Compaction transforms are NOT persisted.** Tier 1 and 1.5 compute the compacted view from raw `messages` + watermarks on every call. The raw `messages` list grows append-only in checkpoints, exactly as today.

**Tier 3 output IS persisted** via the `summary_marker` state field. Once written, subsequent calls read from `messages[summarized_through_turn_index:]`, prepending the persisted marker. The raw pre-summary messages remain in state — retained for redrive, audit, and UI rendering — but are never re-fed to the LLM.

**Redrive semantics:**

- Redrive from the **last** checkpoint: watermarks and summary_marker are restored; compaction state continues monotonically. No re-summarization.
- Redrive with **rollback_last_checkpoint** (Phase 2 Track 2): watermarks roll back with the rest of state. If the rollback target is pre-Tier-3, the summary_marker is `None`; if it is post-Tier-3, the marker is restored. No cross-checkpoint contamination.
- Follow-up (new task, seeded from prior): a follow-up task starts with fresh state (watermarks at 0, summary_marker None). Prior compaction is not inherited — the new task's history is a fresh slate, and pre-Tier-3 memory flush can fire on its first crossing.

## Customer-visible behavior changes

The task-detail API response (`GET /v1/tasks/{id}`) exposes message history via the task's checkpoint. Track 7 changes what customers see on that endpoint:

- **Tool results larger than 25KB are head+tail truncated** at ingestion. Customers who rely on verbatim tool output for audit, replay, or debugging will see `[... truncated N bytes ...]` markers in place of the middle content. Head and tail portions remain verbatim.
- **Older tool-result content is masked** (Tier 1) once the task crosses the Tier 1 threshold. The placeholder (`[tool output not retained — {tool} returned {N} bytes at step {i}]`) gives enough anchor to identify which tool ran but not re-read its output. Customers wanting a full audit trail should pull the structured `compaction.per_result_capped` / `compaction.tier1_applied` logs from Langfuse, which preserve the original byte counts and tool names.
- **Older tool-call arguments are truncated** (Tier 1.5) — large `content`/`new_string`/`body`/etc. args in old `tool_calls` records become `[N bytes — arg truncated after step i]`.
- **On Tier 3 firing, a `SystemMessage` summary marker replaces the oldest prefix** in the LLM input view. Raw pre-summary messages are retained in state for audit/redrive; they just aren't sent to the LLM. The task-detail API can choose to show the marker OR the raw messages; document the chosen behavior in the API surface.

**This is a breaking change for any customer relying on verbatim tool history in the task-detail response.** Release notes + STATUS.md update must call it out. Expected impact: audit consumers already have structured logs as an alternative, and no public API contract guarantees byte-for-byte preservation of tool results in state.

## Observability

**Structured log events** (all emit `tenant_id`, `agent_id`, `task_id`, `step_index`):

| Event | When | Payload |
|-------|------|---------|
| `compaction.per_result_capped` | Tool wrapper caps a ToolMessage | `tool`, `orig_bytes`, `capped_bytes` |
| `compaction.tier1_applied` | Tier 1 advanced `cleared_through_turn_index` | `messages_cleared`, `watermark_before`, `watermark_after`, `est_tokens_saved` |
| `compaction.tier15_applied` | Tier 1.5 advanced `truncated_args_through_turn_index` | `args_truncated`, `bytes_saved` |
| `compaction.tier3_fired` | Tier 3 ran the summarizer | `summarizer_model`, `turns_summarized`, `summary_bytes`, `cost_microdollars`, `latency_ms` |
| `compaction.tier3_skipped` | Tier 3 threshold hit but summarizer call failed | `reason`, `retries_exhausted` |
| `compaction.memory_flush_fired` | Pre-Tier-3 memory flush inserted | (no payload — flag-based) |
| `compaction.context_exceeded_irrecoverable` | Hard floor hit | `est_input_tokens`, `model_context_window`, `floor_reason` |

**Langfuse spans:**

- Tier 1 + Tier 1.5 combined: one span `compaction.inline` per agent-node call when any transform fires. Attributes: `est_tokens_saved`, `watermarks_advanced`.
- Tier 3: one span `compaction.tier3` wrapping the summarizer LLM call. Nests the summarizer `LLMSpan` with full prompt/response for ops debugging. Attributes: `summarizer_model`, `turns_summarized`, `cost_microdollars`.
- Pre-Tier-3 flush: one span `compaction.memory_flush` annotating the one-shot system message.
- Per-result cap: one annotation event on the parent tool span when the cap fires.

**Metrics (Langfuse / internal):**

- Per-agent rolling hour: `tier1_fire_rate`, `tier3_fire_rate`, `avg_tokens_saved_per_call`, `cache_hit_rate_ratio` (pre-Track-7 baseline / current).
- Per-task: max `cleared_through_turn_index`, total `compaction_cost_microdollars`, whether `memory_flush_fired_this_task`.

**Dashboards deferred** — raw Langfuse query UI is sufficient for v1.

## Validation and consistency rules

1. **Monotonicity invariant.** Every watermark field reducer is `max` (or strict-append for `summary_marker`). A bug that tried to regress a watermark is rejected at the schema level.
2. **Cache stability invariant.** For any two LLM calls C1 < C2 within a task, the compacted prefix up to `min(watermark_at(C1), watermark_at(C2))` MUST be byte-identical. Enforceable by unit test: run the compaction transform twice on the same input state; assert `output == output`.
3. **Rollback byte-identity.** After Phase 2 Track 2's `rollback_last_checkpoint` restores a prior checkpoint, the compacted prefix generated on the subsequent agent-node call MUST be byte-identical to the compacted prefix generated from that same checkpoint when it was originally written. Enforceable by integration test — compact, checkpoint, advance, rollback, compact, assert equality.
4. **`exclude_tools` is intersection-safe.** Platform exclude list + agent exclude list is a union; no override semantics needed.
5. **Summary marker content is not replayed to the summarizer.** When Tier 3 fires a second time in the same task, the summarizer sees only the new slice being summarized, not the existing marker. This prevents summary-of-summary drift.
6. **Summary marker is strict-append.** The reducer rejects any write where the new value does not start with the old value. Replace is not a valid path; regenerating the marker requires explicitly clearing it (not in scope for v1).
7. **Per-tool-result cap is indivisible from ingestion.** Tool wrapper MUST apply the cap before returning the `ToolMessage`. Uncapped tool results must never enter state.
8. **Memory-disabled agents never fire the pre-Tier-3 flush.** Regardless of `context_management.pre_tier3_memory_flush` value.
9. **Heartbeat / recovery turns never fire the pre-Tier-3 flush.** Detection rule: `len(messages) == last_super_step_message_count` (no `ToolMessage` or `HumanMessage` added since the last agent super-step).
10. **Transforms never mutate input messages or their nested objects.** All `AIMessage.tool_calls`, `ToolMessage.content`, and `args` dict modifications MUST produce new objects via LangChain's `model_copy(update=...)` or equivalent. Mutation has caused cache-invalidation bugs in other tracks.

## Scale and operational plan

**Target task profile:** tasks up to 500 tool invocations should complete without hitting the hard floor. With `PER_TOOL_RESULT_CAP_BYTES=25000` and a 200K-context model, 500 invocations × 25KB = 12.5MB of raw content; Tier 1 masking collapses this to ~500 × 120 bytes (placeholder length) = 60KB in the masked-prefix region, leaving > 190K for protection window + current turn. Comfortable.

**Target cache hit rate:** the compaction transform is deterministic and monotone, so cache-hit rate should approach the pre-Track-7 baseline within 5% for any task whose watermarks advance linearly. Measurement: compare cached-token-fraction from Langfuse before and after enabling Track 7 on the same agent.

**Rollout is a traditional deploy + watch.** Track 7 is always-on for all agents from the moment the worker ships; there is no per-agent config toggle and no phased config-driven canary. If metrics show a regression, roll back the deploy — the same pattern as every other Phase 2 track.

1. **Staging.** Deploy to staging. Run synthetic long-running tasks that cross the Tier 1 threshold. Verify metrics: `tier3_fire_rate` < 1 per 100 LLM calls on the synthetic load; `cache_hit_rate_ratio` within 5% of pre-Track-7 baseline; `compaction.summary_marker_non_append` count is zero.
2. **Production.** Deploy to prod. Track 7 activates for every new and in-flight task on the next `_build_graph` call. Continue watching the same metrics.
3. **Metrics** watched continuously. If Tier 3 fires more than 1 per 100 calls on average, lower `TIER_1_TRIGGER_FRACTION` (clear earlier) before considering the deferred `aggressive_compaction` per-agent override. If any of the rollout metrics regresses materially, roll back the deploy.

**Regression test gate:** the cache-stability invariant (running the pipeline twice on the same state produces byte-identical output — AC 5) is the primary correctness gate for the compaction transforms. Every compaction unit test runs once; no doubled matrix.

## Cross-track coordination

- **Track 2 (Runtime State Model):** adds `context_exceeded_irrecoverable` to the `dead_letter_reason` CHECK constraint (it is a TEXT column + CHECK, not a Postgres enum — see Task 10 for migration shape). Small schema addition; picked up by the Track 7 exec plan.
- **Track 3 (Scheduler and Budgets):** adds `compaction.tier3` to the per-step budget carve-out list alongside `memory_write`. No new enforcement mechanism — reuses the existing named-node carve-out.
- **Track 4 (BYOT):** no action required. Custom MCP tool results automatically flow through the per-tool-result cap.
- **Track 5 (Memory):** adds `context_management.pre_tier3_memory_flush` as an opt-in dependency on memory. Integration point is narrow: one SystemMessage insertion + one state flag. No schema change on `agent_memory_entries` or tools.
- **Track 8 (Coding-Agent Primitives, proposed):** independent rollouts. When Track 8 lands, `sandbox_edit.new_string` / `old_string` arg keys join `TRUNCATABLE_TOOL_ARG_KEYS` via a one-line constants update.
- **Track 9 (Planning Primitive, proposed):** planning state (`plan: list[PlanItem]`) is auto-injected post-compaction by Track 9's own design — Track 7 does nothing special. The cost-stability invariant applies.

## Acceptance criteria

1. All agents (Track 7 is always-on) serve LLM calls with raw history below the Tier 1 threshold and with masked/truncated history above it — verifiable by observing `compaction.tier1_applied` in Langfuse during a long task.
2. The per-tool-result cap is applied before a `ToolMessage` enters state. A BYOT tool returning 500KB is visible in state as ~25KB; the structured log `compaction.per_result_capped` is emitted.
3. Tier 3 fires only when Tier 1 + 1.5 together cannot bring input below `TIER_3_TRIGGER_FRACTION`. Verifiable by test that constructs a synthetic history and confirms the tier ordering.
4. Watermark fields on graph state only advance — a unit test that feeds back a regressing watermark confirms the reducer ignores it.
5. Cache-stability invariant: running the same compaction pipeline on the same state twice produces byte-identical output.
6. `exclude_tools` entries are never masked. Given a task with `memory_note` results scattered through history, after Tier 1 runs, every `memory_note` `ToolMessage` retains its original content.
7. Pre-Tier-3 memory flush fires at most once per task. Fires for agents with `memory.enabled=true AND pre_tier3_memory_flush=true`. Does not fire on heartbeat / recovery turns.
8. `summary_marker` is append-only. A second Tier 3 firing within the same task appends a new summary rather than rewriting the existing one; cache-hit rate on the marker region stays high.
9. Tier 3 cost lands in `agent_cost_ledger` tagged `compaction.tier3`, attributed to the current task and checkpoint.
10. Budget carve-out: tasks with `budget_max_per_task` close to Tier 3 cost do not pause mid-summarization. Verifiable by integration test.
11. Dead-letter with reason `context_exceeded_irrecoverable` transitions the task cleanly, including emitting `task_dead_lettered` event with the new reason.
12. `POST/PUT /v1/agents` validates `context_management` fields per the rules in §Agent config extension. `summarizer_model` pointing at an inactive / wrong-provider model returns 400.
13. Memory-disabled agents never fire the pre-Tier-3 flush, even if `pre_tier3_memory_flush=true` in their config.
14. A Langfuse trace of a task that exercised all three tiers shows one `compaction.tier3` span per firing, one `compaction.inline` span per call that fires tier 1/1.5, and per-result cap annotations on affected tool spans.

## References

- [GitHub issue #50](https://github.com/shenjianan97/persistent-agent-runtime/issues/50) — original proposal, failure modes observed in production.
- Anthropic Cookbook — "Context engineering: memory, compaction, and tool clearing" (March 2026). Source of `clear_tool_uses_20250919` / `compact_20260112` primitives and the `keep`/`trigger` knob shape.
- Manus — "Context Engineering for AI Agents" (July 2025). Source of KV-cache preservation as dominant cost lever, file-system-as-restorable-context, and "mask don't remove" rule.
- JetBrains Research + "The Complexity Trap" (arXiv 2508.21433, NeurIPS DL4C 2025). Empirical evidence that observation masking beats LLM summarization on cost and solve rate.
- Cognition "Don't Build Multi-Agents" (June 2025). Source of the silent-compaction rule ("context anxiety").
- LangGraph `pre_model_hook` + `trim_messages` + LangMem `SummarizationNode` — baseline primitives we build above.
- LangMem summarization guide — https://langchain-ai.github.io/langmem/guides/summarization/.
- Track 5 design — `docs/design-docs/phase-2/track-5-memory.md` — shape precedent for per-agent config, state extension, and opt-in gating.
- Track 2 design — `docs/design-docs/phase-2/design.md §5` — `dead_letter_reason` enum location.
- Track 3 design — `docs/design-docs/phase-2/design.md §2` — per-step budget enforcement and named-node carve-outs.
