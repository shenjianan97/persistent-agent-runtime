# Compaction Mechanisms in `cc-haha`

A survey of every mechanism that shrinks, rewrites, redirects, or otherwise compacts conversation state between the client and the Anthropic API. Written against the tree at `/Users/shenjianan/Project/cc-haha` (main branch, commit `5a86ab0`).

---

## 1. Framing: Three Distinct "Contexts"

Compaction discussions get muddled when "context" means multiple things at once. This codebase operates on three distinct layers:

| Layer | What it is | Persists across turns? |
|---|---|---|
| **Local `messages[]`** | The client-side transcript array. Powers REPL rendering, `/resume`, transcript writes. | Yes, on disk (session transcript) |
| **Wire payload** | The JSON body of each HTTP POST to the API. | No — rebuilt each turn from `messages[]` plus directives |
| **Effective model context** | The token stream attention actually runs over. | No — determined per-request by wire + server cache + server-side directives |

A mechanism can touch any subset of these three. The most interesting trick in the codebase (cached microcompact) touches only the third. The simplest (time-based microcompact) touches all three.

---

## 2. Master Summary Table

| # | Mechanism | When it fires | What it changes | Cache-stable? | Key files |
|---|---|---|---|---|---|
| 1 | **Per-tool size persistence** | Tool result > `maxResultSizeChars` (default 50K chars, hard cap 400KB) | `messages[]` + wire: block content → `<persisted-output>` preview + filepath. Full content → disk. | ✅ Yes, from turn 1 onwards | `src/utils/toolResultStorage.ts:272`, `src/constants/toolLimits.ts:13` |
| 2 | **Per-message aggregate budget** | Parallel tool_results in one user-message total > 200K chars | Same as #1, applied to the largest fresh blocks until under budget | ✅ Yes (frozen decisions via `seenIds`) | `src/utils/toolResultStorage.ts:769` |
| 3 | **Time-based microcompact** | Gap since last assistant > 60 min (GB-flagged, off by default) | `messages[]` + wire: compactable tool_results' content → `"[Old tool result content cleared]"` literal. Keeps last 5. | ❌ No — but cache was already cold | `src/services/compact/microCompact.ts:446`, `timeBasedMCConfig.ts` |
| 4 | **Cached microcompact** (ant-only, stubbed in this build) | Count-based trigger on registered tool_results | Wire only: appends `cache_edits` directive; server drops tool_results from cached prompt. `messages[]` untouched. | ✅ Yes — designed for it | `src/services/compact/microCompact.ts:305`, `cachedMicrocompact.ts` (stub) |
| 5 | **API-native context management** (ant-only) | `input_tokens` > 180K (target: shrink to 40K) | Wire only: `context_management` body param with `clear_tool_uses_20250919` and/or `clear_thinking_20251015`. Server-side. | ✅ Yes | `src/services/compact/apiMicrocompact.ts` |
| 6 | **Session memory compact** | Token threshold, runs *before* legacy autocompact | `messages[]`: persistent markdown in `~/.claude/session-memory/*.md` + pruned message tail + boundary marker | ❌ No | `src/services/compact/sessionMemoryCompact.ts`, `src/services/SessionMemory/` |
| 7 | **Autocompact (legacy)** | Token count > effective window − 13K (or `/compact` command, or PTL retry) | `messages[]`: full rewrite into `[boundary, summary, recentTail, attachments, hooks]` | ❌ No (cache baseline reset) | `src/services/compact/autoCompact.ts`, `compact.ts:387` |
| 8 | **Session memory (async)** | Every ~3 turns or 50K tokens since last extraction (background) | External disk file (`~/.claude/session-memory/*.md`); cross-session | N/A — not wire-affecting | `src/services/SessionMemory/` |
| 9 | **Prompt cache break detection** | Post-response, if `cacheReadTokens` drop > 5% and > 2K | Neither. Observational only — logs cause (model flip, tool schema change, etc.) | N/A | `src/services/api/promptCacheBreakDetection.ts` |
| 10 | **Collapse / group (display only)** | Rendering time | UI only — groups read/search tool calls into `⎿` summary lines | N/A | `src/utils/collapseReadSearch.ts`, `src/utils/groupToolUses.ts` |
| 11 | **`normalizeMessagesForAPI`** | Every API call | Wire only: filters progress/synthetic messages, merges consecutive user messages, strips `tool_reference` blocks, injects boundary markers | Designed to be | `src/utils/messages.ts:1989` |
| 12 | **Cache breakpoint placement** | Every API call | Wire only: places exactly one `cache_control` marker at last-or-second-to-last message | Designed to be | `src/services/api/claude.ts:3063` |

---

## 3. Detailed Breakdown

### 3.1 Per-tool size persistence

**Trigger.** Each tool has a `maxResultSizeChars` declaration. The effective threshold is `min(toolDeclaredMax, DEFAULT_MAX_RESULT_SIZE_CHARS)` where the default is **50,000 chars** (`constants/toolLimits.ts:13`). A GrowthBook flag `tengu_satin_quoll` can override per-tool. Tools that opt out with `Infinity` (notably `Read`) are never persisted — that would create an infinite loop since Read itself is how the model reads persisted content back.

**Action.** The full tool_result content is written to `projectDir/sessionId/tool-results/<tool_use_id>.<txt|json>` via `persistToolResult` (`toolResultStorage.ts:137`). The wire block's content is replaced with:

```
<persisted-output>
Output too large (127.3 KB). Full output saved to: /path/to/file

Preview (first 2.0 KB):
<first ~2KB of original, cut at newline boundary>
...
</persisted-output>
```

Previews are 2KB (`PREVIEW_SIZE_BYTES`) cut at the nearest newline via `generatePreview` (`:339`).

**Model access.** The model reads the file via the `Read` tool with `offset`/`limit`. Because Read is exempt from persistence (`maxResultSizeChars: Infinity`, short-circuited at `toolResultStorage.ts:62`), the round-trip works — if Read's own output got persisted too, the model would be reading-a-file-about-a-file-about-a-file and the loop never terminates.

**Why chunked paging via Read instead of one big inline result.** Several compounding benefits, roughly in order of importance:

1. **Preview usually suffices.** 2KB cut at a newline boundary is enough for most outputs — grep's first hits, shell status, web fetch's opening paragraphs. Paying full token cost 100% of the time for a <20% benefit is wasteful.
2. **Prompt-cache friendliness.** The preview + filepath string is small, deterministic, and frozen — byte-identical on every subsequent turn, cache-stable forever.
3. **Context-window budget.** One 350KB tool result would crowd every subsequent turn's context until a later compaction rescued it. Chunked paging keeps per-turn footprint low.
4. **Selective access.** The model can offset/limit to a specific region, grep the file for a keyword, or skip irrelevant sections — beats scan-from-top every turn.
5. **API ceilings.** Per-block and per-request token ceilings make 400KB inline impossible; paged reads fit.
6. **Retry-ability.** The file persists on disk at a deterministic path even if the turn errors out or the user interrupts. No re-running the original (non-deterministic) tool.
7. **Capture vs. consume separation.** Producing bytes (disk I/O, cheap) is split from consuming them (tokens, expensive). Only the model's judgment spends the expensive one.

The net effect: an agent session can ingest arbitrarily large tool outputs — long diffs, web page dumps, command logs — without the conversation's token budget scaling with them. Disk absorbs the size; in-context footprint stays ~2KB per large result regardless of original size.

**Cache stability.** Persistence happens on turn T and the preview string is byte-identical on every subsequent turn. `fs.writeFile` uses `flag: 'wx'` (`:162`) to avoid re-writing when microcompact replays original messages.

**Benefits** (the user-facing "why"):
1. Model usually doesn't need more than the 2KB preview → avoid paying full token cost 100% of the time for a benefit that materializes <20% of the time.
2. Preview + filepath is small, deterministic, cache-friendly forever.
3. One big tool call doesn't blow the entire conversation's token runway.
4. Selective access (Read with offset, Grep on the filepath) beats sequential scan.
5. API has per-block and per-request token ceilings; chunked reads fit, 400KB inline doesn't.
6. Retry / debug: artifact is on disk at a deterministic path even if the turn errors out.

### 3.2 Per-message aggregate budget

**Trigger.** When parallel tool_results in a single user-message total more than **200,000 chars** (`MAX_TOOL_RESULTS_PER_MESSAGE_CHARS`, `toolLimits.ts:49`). GrowthBook flag `tengu_hawthorn_window` can override, gated by `tengu_hawthorn_steeple`.

**Vocabulary: "message" means one API-level `user` message containing multiple tool_result blocks.** When the assistant calls N tools in parallel, the results come back as a single user message with N tool_result blocks. That's the unit the budget evaluates. Sequential tool calls across turns each live in *separate* user messages and are checked independently.

- Budget-relevant: 10 parallel greps in one turn → one user message with 10 blocks → summed.
- Budget-irrelevant: 10 sequential greps across 10 turns → each its own message → each checked alone.

`collectCandidatesByMessage` (`toolResultStorage.ts:600`) mirrors `normalizeMessagesForAPI`'s consecutive-user-message merging so the budget groups the same way the wire does.

**Three-way partition before selection.** `partitionByPriorDecision` (`:649`) sorts each message's candidates:

- **mustReapply** — previously persisted. Re-apply the cached preview string via Map lookup. Zero I/O, byte-identical, cannot fail.
- **frozen** — previously seen *unreplaced*. Off-limits: retroactively persisting would change a prefix already in cache and break the suffix.
- **fresh** — never seen. Only these can be selected for new replacement.

Every new turn adds at most one user message with fresh blocks; every prior message is 100% mustReapply + frozen. The budget check almost always just re-applies cached previews.

**Greedy selection algorithm** (`selectFreshToReplace:675`):

```ts
const sorted = [...fresh].sort((a, b) => b.size - a.size)
let remaining = frozenSize + fresh.reduce((s, c) => s + c.size, 0)
for (const c of sorted) {
  if (remaining <= limit) break
  selected.push(c)
  remaining -= c.size
}
```

Sort fresh blocks by size descending, pick the biggest, subtract its size, stop when total is under budget. One or many blocks get persisted per message depending on how far over budget it is.

Example: `[120K, 60K, 40K, 30K, 20K, 15K, 10K, 5K]` = 300K total, limit 200K. Pick 120K → remaining 180K → under limit → stop. **One** block persisted. The 120K becomes `<persisted-output>`; the other seven pass through unchanged.

Example: `[80K, 80K, 80K, 80K, 80K]` = 400K. Pick 80K (320K), pick 80K (240K), pick 80K (160K, under), stop. **Three** blocks persisted.

**Each persisted block is transformed independently** — its own filepath, its own 2KB preview of *its* content. Non-selected blocks in the same message are entirely unchanged.

**Accepted overage.** If frozen alone > limit (possible after flag-change or pre-enablement history), fresh gets fully shed and the message still exceeds budget. Comment at `:672`: "accept the overage — microcompact will eventually clear them."

**Tools exempt.** `Read` (and any tool with `maxResultSizeChars: Infinity`) is added to `seenIds` as frozen, never counted in `freshSize`, never selected (`:780`). Prevents the read-about-read loop.

**Cache stability via frozen decisions.** Every tool_use_id's fate is decided on first pass and recorded in `state.seenIds` (`:390`). Replacements go in `state.replacements` as the exact preview string. Subsequent turns re-apply by Map lookup — byte-identical, zero I/O, cannot fail. Prefix cache survives.

**Resume.** Decisions survive `/resume` via transcript records (`ContentReplacementRecord`, `:475`). Fork subagents inherit parent's replacements so cache-sharing forks make identical choices.

### 3.3 Time-based microcompact

**Trigger.** `evaluateTimeBasedTrigger` (`microCompact.ts:422`): when `(now − lastAssistantTimestamp) > gapThresholdMinutes` (default **60 min**), main-thread only, flag `tengu_slate_heron` with `enabled: false` default. The 60-min choice aligns with the server's 1h cache TTL — if it's been that long, the cache has expired anyway.

**Action.** Keeps the last `keepRecent` (default 5) compactable tool_results, replaces the rest's content with the literal string `"[Old tool result content cleared]"` (`:36`). Only applies to a whitelist: `Read`, shell tools, `Grep`, `Glob`, `WebSearch`, `WebFetch`, `FileEdit`, `FileWrite` (`:41`). Other tool results (`Task`, `TodoWrite`, MCP calls, etc.) are left alone.

**No reference left behind.** Unlike size-based persistence, the cleared message contains *no filepath* — the content is simply gone from the model's view. The original will still be in the on-disk transcript (for resume/display), but the model can't recover it mid-session.

**Side effects.** Calls `resetMicrocompactState()` (`:517`) because the cache break invalidates any pending cached-MC state, and `notifyCacheDeletion()` so the break-detector doesn't flag the expected cache drop as an incident.

### 3.4 Cached microcompact (ant-only, stubbed in this build)

**Scaffolding lives at `microCompact.ts:305`; the real implementation is in `cachedMicrocompact.ts` which is a Proxy-stubbed no-op in external builds** (auto-generated from `scan-missing-imports`; header comment explicitly says `自动生成…ant-internal feature() gated 模块`). Gated by `feature('CACHED_MICROCOMPACT')`, a `bun:bundle` compile-time flag — the entire branch dead-codes out for non-ant builds.

**Mechanism.** Uses Anthropic's beta `cache_edits` API directive:

```ts
type CachedMCEditsBlock = {
  type: 'cache_edits'
  edits: { type: 'delete'; cache_reference: string }[]
}
```

`cache_reference` is the `tool_use_id` of a past tool call. The server drops its cached tool_result from the evaluated prompt while preserving every other cached token.

**Flow per turn:**
1. `registerToolResult` / `registerToolMessage` (`microCompact.ts:324-328`) tracks all compactable tool_results grouped by user-message.
2. `getToolResultsToDelete` returns oldest IDs once count exceeds `triggerThreshold`, keeping the most recent `keepRecent`.
3. A `cache_edits` block gets queued in `pendingCacheEdits` (`:338`).
4. `addCacheBreakpoints` (`claude.ts:3141`) splices it into the last user message and calls `pinCacheEdits(i, ...)`.
5. Every subsequent turn, `getPinnedCacheEdits()` re-inserts all prior edits at their original `userMessageIndex` (`claude.ts:3127-3139`).

**Why the pins.** If turn T+1 dropped the `cache_edits` block, the cached prefix hash wouldn't match → cache miss → all benefit lost. And without re-applying the edit, the server would re-hydrate the deleted tool_results into the prompt.

**What actually changes where:**
- `messages[]`: unchanged (`:369` comment explicitly).
- Wire payload: slightly *longer* (cache_edits directives append to it).
- Model's effective context: *shorter* — attention runs over the edited prompt after the server applies the deletions.

**This is the one mechanism in the codebase that decouples wire-size from model-context-size.**

### 3.5 API-native context management

**`apiMicrocompact.ts`** — a feature of the Anthropic API itself. The *server* does the compaction based on policies declared in the request body. The version-dated type names (`_20250919`, `_20251015`) are Anthropic-side behavior contracts that evolve with the platform.

**Wire format:**

```ts
context_management: {
  edits: [
    {
      type: 'clear_tool_uses_20250919',
      trigger: { type: 'input_tokens', value: 180_000 },
      clear_at_least: { type: 'input_tokens', value: 140_000 },
      clear_tool_inputs: ['Bash', 'Grep', 'Read', ...]
    },
    {
      type: 'clear_thinking_20251015',
      keep: 'all'
    }
  ]
}
```

**Two strategy types:**

**`clear_tool_uses_20250919`** — triggers at `input_tokens > 180K` (`API_MAX_INPUT_TOKENS`), sheds at least 140K worth of tool content so the effective prompt lands near 40K (`API_TARGET_INPUT_TOKENS`). Two modifier modes:

- **`clear_tool_inputs: [whitelist]`** (`useClearToolResults` branch, `:104-125`) — server may drop inputs AND outputs of listed tools. The whitelist is read-only / re-runnable tools: `SHELL_TOOL_NAMES`, `GLOB`, `GREP`, `READ`, `WEB_FETCH`, `WEB_SEARCH`. If the model needs the info back, it can just re-invoke.

- **`exclude_tools: [blocklist]`** (`useClearToolUses` branch, `:128-150`) — server may clear tool uses of everything *except* these. The excluded list is mutating tools: `FILE_EDIT`, `FILE_WRITE`, `NOTEBOOK_EDIT`. Their tool_use records are irreplaceable — "I edited foo.ts line 42" can't be reconstructed and the mutation already happened on disk. Preserving them maintains the audit trail.

Naming note: `TOOLS_CLEARABLE_USES` (the constant holding `[FILE_EDIT, FILE_WRITE, NOTEBOOK_EDIT]`) is confusingly named — it's actually the list of tools whose uses are **not** clearable (because they go into `exclude_tools`). Reads more naturally as "destructive tools whose use records we must preserve."

**`clear_thinking_20251015`** — manages extended-thinking blocks (`:82-87`). Two modes: `keep: 'all'` (default when thinking is active, preserve every turn's thinking) or `keep: { type: 'thinking_turns', value: 1 }` (fires on `clearAllThinking=true`, which is set when idle > 1h — thinking blocks are cheap to drop when the cache is already cold). Skipped entirely when `isRedactThinkingActive` — redacted thinking has no model-visible content to manage.

**Gating:**
- `clear_thinking_20251015` — available to everyone. The ant check at `:90` happens *after* this strategy has already been pushed.
- `clear_tool_uses_20250919` (both modes) — `USER_TYPE === 'ant'` AND env vars `USE_API_CLEAR_TOOL_RESULTS` / `USE_API_CLEAR_TOOL_USES` truthy. External users never see aggressive tool-clearing.

**Key difference from cached microcompact:**
- **Cached-MC** — client decides which `tool_use_id`s to drop (count-based, deterministic). "Keep the last 5 greps, drop the rest."
- **API-native** — client declares a policy; server picks what to drop to meet a token budget. "Just keep us under 40K somehow."

Cached-MC is surgical; API-native is budget-driven. They can run together — cached-MC handles steady-state pruning, API-native kicks in as a safety net.

**Why server-side at all:**
1. **Accurate token counts.** Client can only estimate (~4 bytes/token). Server knows exactly. Client-side trigger would either fire too early (waste) or too late (overflow).
2. **Post-cache-hit evaluation.** Server applies the policy *after* matching the cached prefix, so the wire keeps sending full content for cache matching while the effective prompt shrinks. Doing this client-side would require mutating `messages[]` and breaking the cache.

Both strategies are wire-only — no `messages[]` mutation. Same core trick as cached-MC: decouple wire bytes from model context.

### 3.6 Session memory compact

**A two-part system.** File header marks it `EXPERIMENT` (`sessionMemoryCompact.ts:2`). Off by default — requires BOTH `tengu_session_memory` AND `tengu_sm_compact` GrowthBook flags to be true (default false), or env override `ENABLE_CLAUDE_CODE_SM_COMPACT=1`.

#### Part 1: Session memory (background extraction)

A persistent markdown file at `~/.claude/session-memory/<session>.md`, maintained continuously by a post-sampling hook (`sessionMemory.ts:374`). Fixed template (`prompts.ts:11`) with structured sections:

```
# Session Title
# Current State
# Task specification
# Files and Functions
# Workflow
# Errors & Corrections
# Codebase and System Documentation
# Learnings
# Key results
# Worklog
```

The hook periodically forks a subagent that reads recent history and edits the markdown via the `Edit` tool. Update rules force preservation of section headers + italic template descriptions; only content within sections is edited. "Current State" is flagged as always-refresh for post-compact continuity. Cost is amortized across turns and runs async (doesn't block user interaction).

#### Part 2: SM-compact path (reusing the already-maintained summary)

When autocompact is about to fire, `trySessionMemoryCompaction` (`sessionMemoryCompact.ts:514`) runs before the legacy path. If it succeeds, legacy is skipped.

Output structure:
```ts
{
  boundaryMarker,
  summaryMessages: [<truncated memory markdown wrapped as user message>],
  attachments: [planAttachment?],
  hookResults: [SessionStart hooks],
  messagesToKeep: <recent tail>,
}
```

**No compact API call** — `:498` comment: "SM-compact has no compact-API-call." The summary already exists.

**Tail sizing** (`calculateMessagesToKeepIndex:324`):
- Start at `lastSummarizedMessageId + 1` (anchor from last memory-extraction pass).
- Expand backward until `minTokens` (default 10K) AND `minTextBlockMessages` (default 5) are met.
- Cap at `maxTokens` (default 40K).
- `adjustIndexToPreserveAPIInvariants` ensures tool_use/tool_result pairs aren't split.

#### Problems it solves vs. legacy full autocompact

1. **Blocking latency.** Legacy requires a synchronous summarizer API call at compact time — typically 10-30 seconds of stall during which the user is waiting. SM-compact needs no API call; the summary already exists.
2. **Compaction cost.** Legacy summarizer reads 180K+ input tokens per compact. SM-compact's incremental updates each process only ~50K of new content, amortized across turns, non-blocking.
3. **Summary quality / drift.** Legacy's one-shot summarize-under-pressure loses detail; repeated compactions summarize-of-summary, compounding loss. SM-compact's structured template resists this — "Errors & Corrections" accumulates across turns via edits, never re-summarized.
4. **Cross-session continuity.** Legacy summaries live in the transcript and don't survive session exit. SM's markdown file persists on disk for a future `/resume` or a fresh session referencing the same work.
5. **No verbatim tail in legacy.** Legacy full-compact keeps zero messages verbatim. SM-compact keeps 10-40K tokens of recent messages untouched.

#### Fallback paths

Each logs a distinct event, each returns `null` to caller which falls through to legacy `compactConversation`:

- Flags off (`shouldUseSessionMemoryCompaction` returns false).
- `getSessionMemoryContent()` returns nothing (file never created).
- `isSessionMemoryEmpty` (file exists but just the unfilled template).
- `lastSummarizedMessageId` not found in current messages (stale after edit/fork).
- Resulting `postCompactTokenCount >= autoCompactThreshold` (memory too long, would re-trigger immediately).

#### Two scenarios handled

- **Normal case** (`:548-560`): `lastSummarizedMessageId` set → keep messages after that anchor + backward expansion.
- **Resumed session** (`:561-566`): memory has content but no anchor (likely pre-resume origin) → fall back to using memory as the full summary, no messages kept initially.

#### The architectural bet

Continuous, cheap, incremental, asynchronous summarization > one-shot, expensive, blocking summarization. For short sessions, SM-compact wastes background cycles. For long sessions, the trade heavily favors it: latency saved, tokens saved, quality preserved, cross-session continuity gained.

### 3.6.1 Does autocompact keep the last N turns of tool results?

Frequent question; deserves an explicit answer. **The main automatic path does not. Only SM-compact and manual partial compact preserve a tail.**

| Path | Keeps recent tail? | Sizing |
|---|---|---|
| Session memory compact (tried first if enabled) | ✅ Yes | Token/text-block budget (10-40K tokens typically) |
| Full autocompact (`compactConversation`, fallback) | ❌ No | `messagesToKeep` never set; `buildPostCompactMessages` produces `[boundary, summary, attachments, hooks]` |
| Manual partial compact with `direction: 'up_to'` | ✅ Yes | User-chosen pivot index |

The full autocompact's `CompactionResult` (at `compact.ts:738-748`) **never sets `messagesToKeep`**. Even though the type declares it `messagesToKeep?: Message[]`, only SM-compact and partial-compact populate it. So when the fallback path runs, every pre-compact message is replaced by summary prose. If the model needs a specific grep's exact output post-compact, it re-runs the grep.

What the full path *does* inject is `postCompactFileAttachments` (`:532-585`): top 5 recently-read files, active skills, tool-schema deltas, agent deltas, MCP deltas, plan mode attachment. These restore *state the model needs to continue* but they are not kept original messages — the tool_use/tool_result for that read is gone.

There is no "keep last N turns" turn-count configuration anywhere. Tailing is always token-sized, all-or-nothing, or user-directed.

### 3.7 Autocompact (legacy full-rewrite)

**Trigger** (`autoCompact.ts:72-91, 160-239`):
- Token count > `getAutoCompactThreshold(model)` = effective-context-window − 13K buffer.
- Not gated off by `DISABLE_COMPACT`, `DISABLE_AUTO_COMPACT`, or `userConfig.autoCompactEnabled: false`.
- Not a subagent/forked-agent (recursion guard via querySource).
- Not in reactive-compact or context-collapse mode.
- Circuit breaker: skips after 3 consecutive failures (`autoCompact.ts:60, 260-265`) to avoid hammering the API on irrecoverable sessions.

**Also manually triggered** by `/compact` command and by PTL (prompt-too-long) retry paths.

**Action** (`compact.ts:387-763`): forks an agent via `streamCompactSummary`, feeds it all pre-compact messages and a 9-section summarization template (`prompt.ts:61-76`). The result replaces `messages[]` with:

```
[boundaryMarker(system) → summaryMessages(user) → messagesToKeep(optional) → postCompactFileAttachments(attachment) → hookMessages(hook_result)]
```

The boundary marker carries `compactMetadata`: `preCompactTokenCount`, preserved-segment UUIDs for relink.

**Post-compact attachments** (`compact.ts:532-585`): top 5 recently-read files restored, active skill content re-attached, tool-schema deltas since compact start, agent deltas, MCP deltas. These backfill state the summary can't convey in plain text.

**Cache stability.** Not stable by default — full array rewrite breaks prefix cache. Optimization attempt: `tengu_compact_cache_prefix` enables "prompt cache sharing" where the summarizer fork reuses the parent's cached prefix (reduces cache_creation cost) but the post-compact prefix itself is new. Cache baseline is reset via `notifyCompaction()` to suppress break-detector false-positives.

### 3.7.1 The `/compact` prompt (deep dive)

The actual summarization prompts used by autocompact and `/compact` live at `src/services/compact/prompt.ts`. This is where the quality of every post-compact session ultimately comes from — worth reading as its own artifact.

#### Three template variants

| Variant | Used by | Summary scope |
|---|---|---|
| `BASE_COMPACT_PROMPT` (`:61-143`) | `/compact` command and full autocompact | "the conversation" (everything) |
| `PARTIAL_COMPACT_PROMPT` (`:145-204`) | Partial compact `direction: 'from'` | "the recent messages" (after pivot) |
| `PARTIAL_COMPACT_UP_TO_PROMPT` (`:208-267`) | Partial compact `direction: 'up_to'` | Everything before pivot; summary precedes kept tail |

All three are assembled via `getCompactPrompt` / `getPartialCompactPrompt` (`:274-303`) as `NO_TOOLS_PREAMBLE + <template> + [Additional Instructions] + NO_TOOLS_TRAILER`.

#### The 9-section output schema (BASE template)

The model is asked to produce summary output structured around nine numbered sections:

1. **Primary Request and Intent** — all explicit user requests and intents in detail.
2. **Key Technical Concepts** — technologies, frameworks, patterns.
3. **Files and Code Sections** — "full code snippets where applicable" plus why each read/edit was important.
4. **Errors and fixes** — with explicit attention to user feedback: "especially if the user told you to do something differently."
5. **Problem Solving** — problems solved and ongoing troubleshooting.
6. **All user messages** (not tool results) — "critical for understanding the users' feedback and changing intent."
7. **Pending Tasks** — outstanding work.
8. **Current Work** — precisely what was being worked on immediately before compaction.
9. **Optional Next Step** — must be "DIRECTLY in line with the user's most recent explicit requests" AND should include **verbatim quotes** from the most recent conversation "to ensure there's no drift in task interpretation."

**Anti-drift engineering.** Two of these sections (4 and 6) plus the detailed-analysis instruction all explicitly call out "user told you to do something differently." And Section 9 hard-codes a direct-quote requirement. These aren't decorative — they fight a specific observed failure mode: **compaction erasing user corrections**. User says "stop doing X" at turn 17; naive paraphrase loses that nuance; post-compact agent resumes doing X. Enumerating raw user messages and quoting recent exchanges verbatim is the cheapest defense against this.

#### The `<analysis>` scratchpad

Before the summary itself, the model is instructed (`:31-44`) to write its reasoning in `<analysis>` tags:

```
<analysis>
1. Chronologically analyze each message and section...
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details: file names, full code snippets, function signatures, file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback...
2. Double-check for technical accuracy and completeness
</analysis>

<summary>
[the 9 sections]
</summary>
```

Then `formatCompactSummary` (`:311-335`) **strips the entire `<analysis>` block** before the summary enters context — only `<summary>` body survives (with tags replaced by a `Summary:` header).

This is structured chain-of-thought: the model plans before committing, but the plan is discarded. Code comment at `:29-30`: "drafting scratchpad that formatCompactSummary() strips before the summary reaches context." You pay the output-token cost for reasoning once, at generation time; future cache reads never carry it.

#### The aggressive no-tools enforcement

Two bookends surround every template: a heavy `NO_TOOLS_PREAMBLE` at the top (`:19-26`) and a `NO_TOOLS_TRAILER` at the bottom (`:269-272`).

**Preamble** (verbatim, placed *before* the "Your task is to..." opening):

```
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
```

**Trailer:**

```
REMINDER: Do NOT call any tools. Respond with plain text only —
an <analysis> block followed by a <summary> block.
Tool calls will be rejected and you will fail the task.
```

**Why it needs to be this aggressive.** The comment at `:12-18` explains. Compact runs with `maxTurns: 1` (`compact.ts:1194`) and inherits the parent's full tool set (required for cache-key matching on the cache-sharing fork path). On Sonnet 4.6+ adaptive-thinking models, the model attempted tool calls anyway **2.79% of the time on 4.6 vs 0.01% on 4.5** — a behavior regression. A denied tool call with `maxTurns: 1` produces no text output → falls through to streaming fallback → wasted API call.

The preamble countermeasures:
1. Placed **first**, before the friendly task description — the strongest instruction-following position.
2. Explicitly enumerates tool names (`Read, Bash, Grep, Glob, Edit, Write`) rather than saying "tools" abstractly.
3. Frames failure concretely (*"REJECTED ... waste your only turn ... you will fail the task"*) rather than abstractly.
4. Repeated at the tail for recency bias.

This is belt-and-suspenders prompt engineering driven by a specific eval-measured regression, not hypothetical safety. The 2.79% number survives as a commit comment because it justified shipping an aggressive, otherwise ugly preamble.

#### The `up_to` variant has different sections

Because a `direction: 'up_to'` summary sits *before* the kept tail on the wire, two sections are renamed:

- Section 8 **"Current Work"** → **"Work Completed"** (past tense — more messages follow)
- Section 9 **"Optional Next Step"** → **"Context for Continuing Work"** (no "next step" — the kept tail *is* the next steps)

Framing also differs: "This summary will be placed at the start of a continuing session; newer messages that build on this context will follow after your summary (you do not see them here)."

#### The post-compact wrapper message

The raw summary isn't inserted as-is. `getCompactUserSummaryMessage` (`:337-374`) wraps it as a user message:

```
This session is being continued from a previous conversation that ran out of context.
The summary below covers the earlier portion of the conversation.

<formatted summary>

If you need specific details from before compaction (like exact code snippets, error
messages, or content you generated), read the full transcript at: <transcript path>

Recent messages are preserved verbatim.    [only when SM-compact or partial-compact]
```

Notable additions:

- **Transcript escape hatch.** The wrapper points to the on-disk transcript of the full pre-compact conversation. If the 9-section summary missed something, the model can Read the raw transcript. Same "preview + reference to source of truth" pattern as per-tool persistence, applied at the conversation level.
- **"Recent messages are preserved verbatim"** only appears when `recentMessagesPreserved=true` (SM-compact and partial-compact paths). Tells the post-compact model not to re-explore work that's already in context as original messages.
- **Follow-up suppression** (`:357-371`, enabled for autocompact): "Continue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with 'I'll continue' or similar." Compaction should be invisible to the user.
- **Proactive/autonomous mode extension** (`:361-368`): if `feature('PROACTIVE')` is on and the agent was running autonomously, adds "you were already working autonomously, continue your work loop" to prevent re-greeting.

#### Custom instructions + hook injection

Users can add their own compact instructions via `customInstructions` (`:284-286`), appended as an `"Additional Instructions:"` section. The BASE template even ships example shapes (`:133-142`):

```
## Compact Instructions
When summarizing the conversation focus on typescript code changes and also
remember the mistakes you made and how you fixed them.
```

Pre-compact hooks can also inject instructions at runtime — `compact.ts:420-422` merges them: `mergeHookInstructions(customInstructions, hookResult.newCustomInstructions)`. So project-level `.claude/` hooks can shape summaries per-repo.

#### Design takeaways from this one prompt

1. **Explicit anti-drift engineering for user corrections.** Two sections plus the analysis instruction all target "user told you to do something differently." Compaction erases nuance; the prompt fights this at the schema level.
2. **Verbatim quote requirement** for "what was I doing right before the break" — paraphrase drift is the failure mode, verbatim is the cheapest defense.
3. **Measurable tool-call regression documented in code.** A `2.79% vs 0.01%` eval delta is quoted inline, justifying the aggressive preamble's existence. Treats the prompt like production code — with scars, commit reasoning, and rollback criteria.
4. **`<analysis>` as drafted-then-discarded CoT.** Structured reasoning at generation time, zero cost at future cache reads. More sophisticated than "think step by step" prompting.
5. **Transcript-path escape hatch.** The summary is lossy by nature; pointing to the raw transcript on disk means the model has a fallback path when summary omissions bite.

The prompt is aggressively opinionated, shows its scar tissue in code comments, and treats summarization as an adversarial production task rather than decoration. Worth studying as its own artifact even if you're prompting for completely different domains.

### 3.8 Session memory (async, background)

Not a compaction mechanism — included for completeness because it overlaps with session memory compact.

- Runs **asynchronously** in a post-sampling hook.
- Extraction thresholds: `turnsSinceLastExtraction >= 3` AND `tokensSinceLastExtraction >= 50K`.
- Updates `~/.claude/session-memory/*.md` incrementally.
- Not wire-affecting; doesn't touch `messages[]`.
- Pipes into session memory compact when that path fires.

### 3.9 Post-compact cleanup

`src/services/compact/postCompactCleanup.ts` — one-time cleanup called after either auto or manual compact succeeds. Clears many module-level caches:

- `microcompactState`
- context-collapse state
- `memoryFiles` cache
- `getUserContext` cache
- system-prompt sections
- classifier approvals
- bash-permission speculative checks
- beta tracing state
- attribution hooks
- session-messages cache

**Notable preservation.** `sentSkillNames` is intentionally NOT cleared — re-injecting full skill listings after compact would be pure cache_creation with marginal benefit.

**Subagent safety.** Skips main-thread resets when invoked from an agent subagent to avoid corrupting parent's module state.

### 3.10 Compact warning hook/state

- `compactWarningState.ts`: external Zustand-like store for a boolean flag.
- `compactWarningHook.ts`: React hook reading it.
- `suppressCompactWarning()` is called immediately after any compact so stale token counts (not yet updated by the next API response) don't flash a bogus "near limit" banner.

UI-only. Doesn't affect compaction logic.

### 3.11 `normalizeMessagesForAPI`

`src/utils/messages.ts:1989-2337` — every API call runs `messages[]` through this before serialization. Does:

- Filters `progress` messages, non-local-command `system` messages, synthetic API errors.
- Merges consecutive user messages (Bedrock compat; 1P does this server-side but normalizing here is deterministic).
- Strips `tool_reference` blocks from tool_result content when tool search disabled.
- Strips tool search beta fields (`caller` from tool_use) unless beta active.
- Merges `system`/`local_command` into adjacent user messages.
- Injects `TOOL_REFERENCE_TURN_BOUNDARY` text sibling when `tool_reference` appears at message tail (prevents ~10% premature stop-sequence sampling on capybara model).
- Relocates tool_reference siblings to prevent two-consecutive-human-turns anomaly (`tengu_toolref_defer_j8m`).
- Filters orphaned thinking-only assistant messages, strips trailing thinking, removes whitespace-only messages.

**Why it matters for compaction.** The per-message budget in `enforceToolResultBudget` must group tool_results the same way `normalizeMessagesForAPI` merges them — otherwise a parallel-tool burst could split into N under-budget groups pre-merge and become one over-budget message post-merge. `collectCandidatesByMessage` in `toolResultStorage.ts:600` has extensive comments explaining this invariant.

### 3.12 Cache breakpoint placement

`addCacheBreakpoints` in `claude.ts:3063-3159`. Places **exactly one** `cache_control` marker per request at message index `length - 1` (or `length - 2` if `skipCacheWrite=true` for fire-and-forget forks). Also handles cache_edits insertion/pinning as described under cached microcompact.

**Single-marker design.** Reduces local-attention KV page eviction vs. multiple markers. The single-marker-at-tail policy paired with append-only growth is what makes the prompt-cache optimization tractable in the first place.

### 3.13 Prompt cache break detection

`src/services/api/promptCacheBreakDetection.ts` — observational, not a compaction mechanism. Two-phase:

**Phase 1** (`recordPromptState`, pre-call): hashes system prompt, tool schemas, cache_control scope/TTL, betas, model, effort, extraBodyParams. Stores pending changes.

**Phase 2** (`checkResponseForCacheBreak`, post-call): if `cacheReadTokens` drops > 5% AND exceeds 2K min, fires. Attributes break to pending changes: model flip, system prompt delta, tool add/remove/schema change, cache_control flip, beta addition, auto/overage/cachedMC toggle, effort change.

**Suppressions:**
- `cacheDeletionsPending=true` (cached microcompact expected a deletion).
- TTL expiry > 1h (server-side, not our fault).
- Haiku models (different caching characteristics).

**Emits** `tengu_prompt_cache_break` analytics + diff file (ants with `--debug` only).

### 3.14 Collapse / group (UI-only)

Two mechanisms that affect *only* rendering, not wire:

- **`collapseReadSearch.ts`**: consecutive Read/Grep/Glob/WebFetch/WebSearch calls collapse into `⎿ (⏱ + count badge)` summary line. Memory-file writes also collapsible; Snip/ToolSearch are "absorbed-silently" (no count bump).
- **`groupToolUses.ts`**: multiple tool_uses of the same name from the same `message.id` (same API response) render as one collapsed block. Skipped in verbose mode.

Neither mutates `messages[]` or wire. Important to call out since "collapsed" in the UI can look like compaction but isn't.

### 3.15 `grouping.ts` (inside compact/)

`groupMessagesByApiRound()` — partitions messages at assistant-message-id boundaries (one group per API round-trip). Used only by compaction/reactive-compact to decide which messages to truncate for PTL retry. The grouping data structure itself isn't sent to the API; it's an intermediate for message-selection logic.

---

## 4. Interaction Map

```
Tool produces result
    │
    ▼
Per-tool size persistence (#1) ──→ content > 50K? → <persisted-output> with filepath
    │                                    │
    ▼                                    └─ on-disk artifact
Per-message aggregate budget (#2) ──→ sum > 200K? → pick largest, persist
    │
    ▼
messages[] (local transcript)
    │
    ▼
[Between turns, on idle]
    │
    ├─ gap > 60min? ──→ Time-based microcompact (#3) mutates messages[] → "[cleared]"
    │
    ├─ count-trigger? ──→ Cached microcompact (#4) queues cache_edits (wire-only)
    │
    └─ tokens > threshold?
           │
           ├─ Session memory compact (#6) first
           ├─   if returns null: Autocompact (#7) full rewrite
           │
           └─ postCompactCleanup (#3.9)
    │
    ▼
normalizeMessagesForAPI (#11) ──→ filter/merge/strip
    │
    ▼
addCacheBreakpoints (#12) ──→ inject cache_control + pinned cache_edits
    │
    ▼
+ context_management body param (API-native MC, #5)
    │
    ▼
Wire payload
    │
    ▼
    ┌─────────────────────────────────────────┐
    │ Server: prefix cache match + apply      │
    │         cache_edits + context_management│
    │         → effective prompt              │
    └─────────────────────────────────────────┘
    │
    ▼
Model attention runs over effective prompt
    │
    ▼
Response
    │
    ▼
checkResponseForCacheBreak (#9) ──→ attribute any cache drop
Session memory async extraction (#8)
```

---

## 5. Benefits by Design Axis

### 5.1 Why persist-with-reference instead of truncate?

Truncation throws away information. Persistence preserves it at a stable, model-addressable path. The model can Read any portion on demand. This separates:

- **Capture cost** (cheap: disk I/O when the tool runs)
- **Consumption cost** (expensive: tokens when the model actually reads)

Inline-everything conflates them, paying consumption cost whether or not the model would have chosen to consume.

### 5.2 Why so many thresholds?

Different failure modes need different handlers:

| Failure mode | Mechanism |
|---|---|
| One huge tool result | #1 per-tool size persistence |
| Many medium parallel tool results | #2 per-message aggregate budget |
| Idle conversation resumed after cache expiry | #3 time-based microcompact |
| Long active session filling context with old tool results | #4 cached microcompact (ideal) / #5 API-native (fallback) |
| Genuinely full context, no tool results to shed | #6 session memory compact → #7 autocompact |

Trying to handle all of these with one threshold produces pathological behavior at the edges (e.g., one 300K result triggering a full summarize; or 10K of accumulated small results surviving forever).

### 5.3 Why prompt-cache-stability drives so much complexity

Anthropic's prompt cache has a 1h TTL and is prefix-keyed. A cache hit on a 100K-token prefix is ~90% cheaper than re-processing. Over a long conversation, cache-stable mechanisms (#1, #2, #4, #5) can save 10–100× on input-token cost vs. naive per-turn full processing. The code's `seenIds`-based frozen-decisions, replacement Map byte-identical re-apply, pinned cache_edits, and circuit-breaker isolation all exist to preserve cacheability.

Time-based microcompact (#3) and autocompact (#7) are the "give up on current cache" paths — used when cache is either already cold (gap > TTL) or the situation is too severe to salvage incrementally.

### 5.4 Why some mechanisms don't change `messages[]`

`messages[]` is the client-side source of truth for rendering, `/resume`, transcript writes, and resumable subagent state. Mechanisms that mutate it (#3, #6, #7) lose information the resumed session can't recover. Mechanisms that operate wire-only (#4, #5, #11, #12) can aggressively shrink what the model sees while preserving everything the user and future sessions can re-access.

---

## 5.5 What's actually enabled in an external build?

Several mechanisms in the table are scaffolding that's dormant for non-ant users. Clear picture of what actually runs:

| Mechanism | External build status |
|---|---|
| Per-tool size persistence (#1) | ✅ Active, always on |
| Per-message aggregate budget (#2) | ⚠️ Requires `tengu_hawthorn_steeple=true` (default unknown, likely on for ants) |
| Time-based microcompact (#3) | ❌ Default `enabled: false` in `tengu_slate_heron` |
| Cached microcompact (#4) | ❌ Stub only — `CACHED_MICROCOMPACT` compile-time feature flag is off; entire module DCE'd |
| API-native `clear_thinking` (#5a) | ✅ Available to everyone when thinking is active |
| API-native `clear_tool_uses` (#5b) | ❌ `USER_TYPE === 'ant'` + env vars required |
| Session memory compact (#6) | ❌ Requires BOTH `tengu_session_memory` AND `tengu_sm_compact` (both default false) |
| Autocompact (legacy #7) | ✅ Active, triggers at context-window − 13K |
| Session memory extraction (#8) | ❌ Requires `tengu_session_memory` (default false) |
| Prompt cache break detection (#9) | ✅ Active via `PROMPT_CACHE_BREAK_DETECTION` compile flag |
| `normalizeMessagesForAPI` (#11) | ✅ Every API call |
| Cache breakpoint placement (#12) | ✅ Every API call |

**Net effect for an external user:** the observable compaction stack is basically (#1 per-tool size persistence) + (#2 per-message budget, conditionally) + (#7 legacy autocompact at 13K-below-window). The sophisticated cache-preserving paths (#4 cached-MC, #5b aggressive API-native, #6 SM-compact) are all ant-internal rollouts.

## 6. The Core Insight (from the user's question)

> "Wire ≠ model context."

The cleanest conceptual takeaway: the Anthropic API lets clients send a "full" prompt while using directives (cache_edits, context_management) to instruct the server to evaluate a *different* prompt. This means:

- Prompt cache prefixes can stay byte-identical (cache hits preserved).
- The model's attention spans a smaller window (context budget preserved).
- The client's `messages[]` can stay complete (resume/transcript preserved).

All three constraints satisfied simultaneously. This is the core design principle that `cc-haha`'s multi-layered compaction stack is optimizing around.

---

## 7. Key Constants Reference

| Constant | Value | Location |
|---|---|---|
| `DEFAULT_MAX_RESULT_SIZE_CHARS` | 50,000 | `src/constants/toolLimits.ts:13` |
| `MAX_TOOL_RESULT_BYTES` | 400,000 (100K tokens × 4 bytes) | `src/constants/toolLimits.ts:33` |
| `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS` | 200,000 | `src/constants/toolLimits.ts:49` |
| `PREVIEW_SIZE_BYTES` | 2,000 | `src/utils/toolResultStorage.ts:109` |
| `TIME_BASED_MC_CLEARED_MESSAGE` | `"[Old tool result content cleared]"` | `src/services/compact/microCompact.ts:36` |
| Time-based MC default gap | 60 min | `src/services/compact/timeBasedMCConfig.ts:32` |
| Time-based MC default `keepRecent` | 5 | `src/services/compact/timeBasedMCConfig.ts:33` |
| Autocompact threshold | context window − 13K | `src/services/compact/autoCompact.ts:62` |
| Autocompact circuit-breaker | 3 consecutive failures | `src/services/compact/autoCompact.ts:60` |
| API_MAX_INPUT_TOKENS (native MC) | 180K | `src/services/compact/apiMicrocompact.ts` |
| API_TARGET_INPUT_TOKENS (native MC) | 40K | `src/services/compact/apiMicrocompact.ts` |
| Session-memory async thresholds | 3 turns + 50K tokens since last | `src/services/SessionMemory/` |
| Cache-break detection min drop | 5% AND > 2K tokens | `src/services/api/promptCacheBreakDetection.ts` |

---

## 8. Feature Flags (GrowthBook)

| Flag | Controls |
|---|---|
| `tengu_satin_quoll` | Per-tool persistence threshold overrides |
| `tengu_hawthorn_steeple` | Per-message aggregate budget enable |
| `tengu_hawthorn_window` | Per-message budget size override |
| `tengu_slate_heron` | Time-based microcompact config |
| `tengu_cache_plum_violet` | Legacy microcompact (always true now; legacy path removed) |
| `tengu_session_memory` | Session memory feature |
| `tengu_sm_config` | Session memory update thresholds |
| `tengu_sm_compact_config` | Session memory compact thresholds |
| `tengu_compact_cache_prefix` | Autocompact summarizer prompt cache sharing |
| `PROMPT_CACHE_BREAK_DETECTION` | Break-detection emit |

Compile-time flags (`bun:bundle`):

| Flag | Controls |
|---|---|
| `CACHED_MICROCOMPACT` | Cached-microcompact module linking (ant-only) |
