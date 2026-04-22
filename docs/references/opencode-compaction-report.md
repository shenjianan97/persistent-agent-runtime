# opencode context-management / compaction investigation

> Scope: the `sst/opencode` coding agent. The working tree is the
> `anomalyco/opencode` fork, which tracks `sst/opencode` upstream — the
> core files referenced below (session/compaction.ts, session/prompt.ts,
> session/message-v2.ts, tool/truncate.ts, provider/transform.ts) are
> the upstream files, shown at commit `d2181e927` on branch `dev`.

---

## 1. Intro

opencode is a TypeScript monorepo (Bun workspace). The CLI/agent runtime
lives in `packages/opencode/`. It is **multi-provider** — model access
goes through Vercel AI SDK (`ai`) with per-provider `@ai-sdk/*` packages
(Anthropic, OpenAI, Bedrock, Google, Vertex, OpenRouter, Gateway, Azure,
GitHub Copilot, DashScope, etc.), plus a custom GitLab workflow model.

The chat loop is `SessionPrompt.runLoop` in
`packages/opencode/src/session/prompt.ts:1305`. Each iteration rebuilds
the full local transcript from the database, runs any pending
compaction/subtask task, and otherwise invokes the model stream via
`LLM.run` at `packages/opencode/src/session/llm.ts:72`. Messages are
serialised to the wire-level `ModelMessage[]` by
`MessageV2.toModelMessagesEffect` in
`packages/opencode/src/session/message-v2.ts:587`, after which
`ProviderTransform.message` (`packages/opencode/src/provider/transform.ts:305`)
applies per-provider fixups (including Anthropic prompt-cache markers).

Context management is concentrated in three files under
`packages/opencode/src/session/`:

- `overflow.ts` — budget math (`usable`, `isOverflow`).
- `compaction.ts` — tail-selection, full-conversation summarisation, and
  tool-output pruning.
- `message-v2.ts` — `filterCompacted` projection that the chat loop uses
  to hide pre-compaction history, and `toModelMessagesEffect` which
  applies the `[Old tool result content cleared]` substitution.

Per-tool-result size handling lives in `tool/truncate.ts` and is
wrapped around every tool by `tool/tool.ts` and `tool/registry.ts`.

---

## 2. Master summary table

| Mechanism | Trigger | Local state | Wire payload | Model-visible context | Cache-stable | Key files |
|---|---|---|---|---|---|---|
| Per-tool-result truncation | Every tool returns > 2000 lines or > 50 KiB | Full output written to `$XDG_DATA/opencode/tool-output/<id>`; stored `output` is preview+hint | Same preview+hint goes over wire | Sees preview + path hint | n/a (happens once, then stable) | tool/truncate.ts:15-16, 71-126; tool/tool.ts:98-112 |
| Bash streaming cap | Bash output exceeds 50 KiB during streaming | File sink opens; tail kept for final output | Final output = streamed tail + truncation hint | Same as wire | n/a | tool/bash.ts:411-569 |
| Read tool cap | `read` of > 50 KiB or > 2000 lines | Output capped with "Use offset=N to continue" hint | Capped content sent | Capped content visible | n/a | tool/read.ts:15-19, 236-256, 309-319 |
| Grep cap | > 100 matches | `truncated: true` in metadata, suffix added | Truncated list sent | Truncated list visible | n/a | tool/grep.ts:11, 102-127 |
| Webfetch cap | Response > 5 MiB | Request rejected | — | — | n/a | tool/webfetch.ts:9, 95-100 |
| Auto-compaction (overflow after turn) | After a step finish, `isOverflow({tokens, model})` true | Inserts a synthetic user message with a `compaction` part; next loop tick runs `compaction.process` | History up to `tail_start_id` replaced by assistant summary | Sees summary + preserved tail + optional auto-continue user message | **No** — rewrites prefix | session/compaction.ts:221-457; session/prompt.ts:1384-1391; session/processor.ts:397-402 |
| Auto-compaction (overflow mid-stream) | Stream errors with `ContextOverflowError` | `needsCompaction = true`; loop returns `"compact"` | Same as above, plus `stripMedia: true` via overflow flag | Media files replaced with `[Attached <mime>: <name>]` text | **No** | session/processor.ts:526-530; session/prompt.ts:1514-1522; session/compaction.ts:242-258, 302-304, 429-432 |
| Manual `/compact` | User invokes slash command | Same as above but `auto: false` | Same | Same | **No** | server/routes/instance/session.ts:536-599; cli/cmd/tui/routes/session/index.tsx:477-502 |
| Tool-output pruning (microcompaction) | End of every chat loop (`runLoop` post-loop hook) | Older tool parts past a 40k-token tail get `state.time.compacted` set | `toModelMessagesEffect` replaces their `output` with `"[Old tool result content cleared]"`, drops attachments | Sees placeholder string | **No** — mutates prefix | session/compaction.ts:171-219; session/prompt.ts:1530; session/message-v2.ts:729-730 |
| `filterCompacted` projection | Every loop iteration when building `msgs` | Hides messages older than the last compaction's `tail_start_id`; fully drops history before a completed compaction with no tail | Wire payload built from filtered set only | Sees only retained window | n/a (idempotent projection) | session/message-v2.ts:931-960; session/prompt.ts:1317 |
| Message merging / dedup | Pre-send, Anthropic-family | Empty text/reasoning parts removed; `tool_use, tool_use, text` split into `[text], [tool_use…]` | Reshaped ModelMessage[] | Same | depends — mutates prefix only for malformed shapes | provider/transform.ts:48-176 |
| Prompt-cache breakpoints | Pre-send, Anthropic/Bedrock/OpenRouter/OpenAI-compat/Copilot/Alibaba | Adds `cacheControl: {type: "ephemeral"}` on first 2 system + last 2 non-system messages | Same | Same | **Yes** — this is the cache mechanism | provider/transform.ts:216-265, 305-320 |
| Per-provider `promptCacheKey`/`setCacheKey` | OpenAI / Azure / OpenRouter / Venice / opencode-hosted GPT-5 | Adds provider option `promptCacheKey = sessionID` | Same | Same | **Yes** | provider/transform.ts:814-847, 912-925 |
| Gateway caching | AI SDK Gateway | Adds `providerOptions.gateway.caching = "auto"` | Same | Same | **Yes** (gateway decides) | provider/transform.ts:926-930 |
| Agent-step cap | `step >= agent.steps` | Appends an assistant message with `max-steps.txt` instructing the model to stop using tools | Wire payload gets extra assistant turn | Model sees cap instruction | n/a | session/prompt.ts:1401-1402, 1489; session/prompt/max-steps.txt |
| Doom-loop guard | Same tool+input 3 times in a row | Asks the user for `doom_loop` permission | Not a context change — a halt | n/a | n/a | session/processor.ts:24, 287-331 |
| Plugin `experimental.chat.messages.transform` | Every LLM turn + every compaction | Arbitrary plugin mutation of `msgs` | Whatever plugins do | Whatever plugins do | plugin-dependent | session/prompt.ts:1471; session/compaction.ts:303 |

---

## 3. Per-mechanism detail

### 3.1 Per-tool-result truncation — `Truncate.output`

`packages/opencode/src/tool/truncate.ts:15-16`:

```
export const MAX_LINES = 2000
export const MAX_BYTES = 50 * 1024
```

`Truncate.output(text, opts, agent)` at `truncate.ts:71-126`:

1. If `lines.length ≤ MAX_LINES && byteLength ≤ MAX_BYTES`, return
   unchanged (`truncated: false`).
2. Otherwise write the full text to
   `$XDG_DATA/opencode/tool-output/<ToolID>` (path constant
   `TRUNCATION_DIR = Global.Path.data + /tool-output`;
   `tool/truncation-dir.ts:4`, `global/index.ts:10`).
3. Return a preview (head or tail, first `MAX_LINES`/`MAX_BYTES`), a
   `…N lines truncated…` marker, and a hint telling the model either to
   use the Task/explore agent (if allowed) or Grep/Read-with-offset.
4. Truncated files older than 7 days are swept by `Truncate.cleanup`
   on a 1-hour schedule (`truncate.ts:13, 50-62, 128-135`).

This wrapper is applied universally:

- Native tools — `Tool.define` wraps `execute` and calls
  `truncate.output(result.output)` post-hoc unless the tool already set
  its own `metadata.truncated` (tool/tool.ts:98-112). `read`, `grep`,
  `webfetch`, and `bash` set their own truncation so `tool.ts` leaves
  them alone.
- File-based user-defined tools — `tool/registry.ts:139` runs
  `truncate.output` on the returned string.
- Plugin-defined MCP tools — `session/prompt.ts:494` runs
  `truncate.output` on joined text parts before the result becomes a
  MessageV2 tool-output.

Note: **this truncation is per tool call, not per turn.** N parallel
tool calls can each return up to 50 KiB of preview + path hints.

### 3.2 Tool-specific caps

- **Bash** (`tool/bash.ts:411-569`) streams output into a ring buffer
  keeping `Truncate.MAX_BYTES * 2 = 100 KiB`. As soon as streamed bytes
  exceed `MAX_BYTES`, the tool opens a file sink and continues writing
  the full output there; the in-memory return is kept to the tail of
  `MAX_LINES`/`MAX_BYTES`. Final payload is `...output truncated...
  Full output saved to: <file>` followed by the tail. Default timeout
  also caps output implicitly.
- **Read** (`tool/read.ts:15-19, 236-256`) caps file reads at
  `DEFAULT_READ_LIMIT = 2000` lines and `MAX_BYTES = 50 * 1024`; each
  line also capped at `MAX_LINE_LENGTH = 2000` chars with a
  `... (line truncated to 2000 chars)` suffix. The tool emits an
  `offset=N` hint so the model can page.
- **Grep** (`tool/grep.ts:11, 102-127`) `MAX_LINE_LENGTH = 2000` for
  per-match excerpts; results hard-capped at `limit = 100` with a
  `showing 100 of N matches` suffix.
- **Webfetch** (`tool/webfetch.ts:9, 95-100`)
  `MAX_RESPONSE_SIZE = 5 * 1024 * 1024`; requests over that are
  rejected outright with an error.

### 3.3 Context budget math — `overflow.ts`

`packages/opencode/src/session/overflow.ts:6-26`:

```
const COMPACTION_BUFFER = 20_000

usable(cfg, model):
  if model.limit.context === 0: 0
  reserved = cfg.compaction?.reserved
           ?? min(20_000, maxOutputTokens(model))
  return model.limit.input
    ? max(0, model.limit.input - reserved)
    : max(0, model.limit.context - maxOutputTokens(model))

isOverflow(cfg, tokens, model):
  if cfg.compaction?.auto === false: false
  if model.limit.context === 0: false
  count = tokens.total
        || input + output + cache.read + cache.write
  return count >= usable(cfg, model)
```

`maxOutputTokens` (`provider/transform.ts:1017-1019`) =
`min(model.limit.output, OUTPUT_TOKEN_MAX)` where
`OUTPUT_TOKEN_MAX = Flag.OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX ||
32_000` (`transform.ts:20`).

`isOverflow` consumes **real provider token counts** — they come from
`finish-step` via `Session.getUsage` (`session/session.ts:262-325`),
which reads `usage.inputTokens`, `usage.outputTokens`,
`usage.inputTokenDetails.cacheReadTokens`,
`inputTokenDetails.cacheWriteTokens` (with per-provider fallbacks for
anthropic/vertex/bedrock/venice). No local tokenisation — opencode
uses a cheap `chars / 4` estimator only for internal bookkeeping in
`compaction.ts` (see 3.6).

### 3.4 Auto-compaction — post-turn path

After each `finish-step` event (`session/processor.ts:357-403`) the
processor checks overflow and, if positive and the current assistant
message isn't itself a summary, sets
`ctx.needsCompaction = true` so the stream is drained via
`Stream.takeUntil` and `runLoop` returns to the top.

`runLoop` then detects no pending compaction task and re-evaluates at
`session/prompt.ts:1384-1391`:

```ts
if (lastFinished && lastFinished.summary !== true &&
    (yield* compaction.isOverflow({ tokens, model }))) {
  yield* compaction.create({ sessionID, agent, model, auto: true })
  continue
}
```

`compaction.create` (`session/compaction.ts:459-482`) writes a fresh
**user message** with a single `CompactionPart`
(`auto: true, overflow?: true`). Next loop iteration picks this up at
`session/prompt.ts:1372` and hands off to `compaction.process`.

### 3.5 Auto-compaction — overflow mid-stream

If the provider rejects the request mid-stream with a context-overflow
error, `MessageV2.fromError` maps it to `ContextOverflowError`
(`session/message-v2.ts:1012-1025, 1044-1052`). `SessionProcessor.halt`
detects this and sets `ctx.needsCompaction = true`
(`session/processor.ts:526-530`), returning `"compact"`. The loop then
calls `compaction.create({ …, auto: true, overflow:
!handle.message.finish })` (`session/prompt.ts:1515-1522`).

`overflow: true` changes behaviour in `compaction.process`
(`session/compaction.ts:242-258`):

- The most recent user message (the one that caused overflow) is
  **removed** from the compaction input and held aside as `replay`.
- Media attachments on the replay message are converted to
  `[Attached <mime>: <file>]` text when re-injected
  (`compaction.ts:386-398`).
- The compaction summarisation itself runs with `stripMedia: true`
  (`compaction.ts:304`, reading `message-v2.ts:665-680, 729-738`).
- Auto-continue prepends a synthetic user note explaining media was
  dropped (`compaction.ts:429-433`).

If even stripping media isn't enough to fit, the compaction assistant
message is marked as an error (`compaction.ts:354-363`).

### 3.6 Tail selection — `select`

`compaction.ts:130-169`:

- `limit = cfg.compaction?.tail_turns ?? DEFAULT_TAIL_TURNS = 2`.
- `budget = cfg.compaction?.preserve_recent_tokens ??
  clamp(floor(usable(model) * 0.25), 2_000, 8_000)` —
  constants `MIN_PRESERVE_RECENT_TOKENS = 2_000`,
  `MAX_PRESERVE_RECENT_TOKENS = 8_000`
  (`compaction.ts:37-38, 45-50`).
- Turns are defined as "every user message that isn't a compaction
  marker"; `turns()` groups messages by these anchors
  (`compaction.ts:52-68`).
- Going backwards through the last `limit` turns it accumulates
  turn sizes (using `Token.estimate(JSON.stringify(model-messages))` —
  the chars/4 heuristic, `compaction.ts:122-128, util/token.ts:3-5`)
  until it would exceed `budget`.
- If the last turn alone is larger than `budget`, it gives up
  ("tail fallback") and summarises the whole history — no tail kept
  (`compaction.ts:150-153`).
- Otherwise it returns `head = messages[0 .. keep.start]` for
  summarisation and `tail_start_id = keep.id` for the retained tail.

### 3.7 Compaction prompt

`compaction.ts:277-301` — default prompt is a templated Goal /
Instructions / Discoveries / Accomplished / Relevant files report,
joined with any plugin-provided `context`. A plugin can replace the
prompt entirely via the `experimental.session.compacting` hook
(`compaction.ts:272-276`). The "compaction" agent itself has its own
system prompt in `agent/prompt/compaction.txt` and permissions set
to `"*": "deny"` (`agent/agent.ts:188-202`), so it can't call tools.

### 3.8 Auto-continue after compaction

If `process` returns `"continue"` **and** `auto` was true,
`compaction.ts:372-452` either:

- replays the overflow message as a fresh user message with media
  stripped (`compaction.ts:372-399`), or
- adds a synthetic user prompt via the
  `experimental.compaction.autocontinue` plugin hook
  (`compaction.ts:401-451`). Default text: `"Continue if you have
  next steps, or stop and ask for clarification if you are unsure
  how to proceed."` The synthetic part carries
  `metadata.compaction_continue: true`, which github-copilot uses to
  mark the next request as agent-initiated
  (`plugin/github-copilot/copilot.ts:365-376`).

### 3.9 `filterCompacted` — the local projection

Every chat loop tick starts with
`msgs = yield* MessageV2.filterCompactedEffect(sessionID)`
(`session/prompt.ts:1317`).

Definition at `session/message-v2.ts:931-960`:

- Walks messages newest → oldest (using `stream` which pages 50 at a
  time — `message-v2.ts:887-899`).
- Collects messages until it hits a **completed compaction** (a user
  message that has a `compaction` part and for which the paired
  assistant message is marked `summary: true, finish, !error`).
- If the completed compaction has a `tail_start_id`, it continues
  walking back until that ID (so recent turns still survive).
- If it has no `tail_start_id`, it stops there — all older history is
  hidden from the next wire payload.

Net effect: a successful compaction permanently replaces the pre-tail
history from the model's point of view, but all data is still in the
database (inspectable via `filterCompacted = false` paths), and can be
reverted with `SessionRevert` (`packages/opencode/src/session/revert.ts`).

### 3.10 `prune` — tool-output microcompaction

**When it runs, in plain terms:** after the LLM finishes responding to
a user request (final answer delivered, no more tool calls pending).
Nothing to do with closing the session — it fires once per user turn
after the agent loop exits. Specifically, `compaction.ts:173-219`,
called at the bottom of `runLoop` after the outer `while` has broken,
via `yield* compaction.prune({ sessionID }).pipe(Effect.ignore,
Effect.forkIn(scope))` (`session/prompt.ts:1530`). `Effect.forkIn`
means it runs in the background — the user gets their response
immediately and prune processes old tool outputs asynchronously.
Effects land on the *next* user message (or possibly a later one,
depending on scheduling).

Timeline:

1. User sends a message.
2. Model streams, calls tools, may loop several times.
3. Model finishes, final text delivered to the user.
4. **Prune runs here, in the background.**
5. The placeholder substitution takes effect on the next request.

Gated by `cfg.compaction?.prune !== false` (default enabled).

Algorithm:

- Walks messages oldest → newest *is wrong* — it walks newest → oldest
  (`for msgIndex = msgs.length - 1; msgIndex >= 0; …`).
- Skips the two most recent user turns (`if (turns < 2) continue`).
- For each completed `tool` part not in `PRUNE_PROTECTED_TOOLS =
  ["skill"]` (`compaction.ts:35`):
  - Stops if the part was already pruned (`time.compacted` set).
  - Adds `Token.estimate(part.state.output)` to a running total.
  - Once the running total exceeds `PRUNE_PROTECT = 40_000`
    (`compaction.ts:34`), older parts are queued for pruning.
- If total queued > `PRUNE_MINIMUM = 20_000` (`compaction.ts:33`),
  every queued part has `state.time.compacted = Date.now()` set and
  is persisted.
- A pruned part's `output` string is not rewritten in the DB. The
  substitution happens at serialisation time:
  `toModelMessagesEffect` checks `part.state.time.compacted` and emits
  `"[Old tool result content cleared]"` with empty attachments
  (`message-v2.ts:729-730`).

So prune is "protect the last ~40 k tokens of tool outputs, and if
there's more than ~20 k tokens older than that, mark it for
replacement." It runs **once per user request, as a forked async job**,
and only ever marks things — never un-marks.

### 3.11 UI-only collapsing vs wire-affecting

The TUI's `routes/session/index.tsx:1249, 1314, 1880` renders
CompactionPart headers and collapses compacted tool outputs visually
(`part.state.time.compacted` -> hide). The same flag drives the
wire-level substitution in `toModelMessagesEffect`
(`message-v2.ts:729-730`), so here the UI and the wire are in sync.

There is no TUI-only "collapse repeated tool calls" that leaves the
wire unchanged — display and payload both see the same filtered
message set.

### 3.12 Prompt-cache breakpoint placement

`provider/transform.ts:216-265, 305-320`. Invoked only for models
matched as Anthropic-family (provider is anthropic/google-vertex-
anthropic, `api.id`/`id` contains `claude`/`anthropic`, or
`api.npm === @ai-sdk/anthropic` / `@ai-sdk/alibaba`) **and** not
using `@ai-sdk/gateway`.

`applyCaching`:

- Picks first 2 system messages and last 2 non-system messages
  (`transform.ts:217-218`). Note: this gives up to 4 distinct
  breakpoints — Anthropic's public limit. Bedrock gets
  `cachePoint: {type: "default"}`; others get
  `cacheControl: {type: "ephemeral"}`.
- For `@ai-sdk/anthropic` / `@ai-sdk/amazon-bedrock` the cache marker
  is set at the **message** level; for everything else it's set at
  the **last content part** of the message
  (`transform.ts:241-262`).
- TTL: always `type: "ephemeral"` (5 min on Anthropic). **Long
  (1h) cache is not used** — `"1h"`, `extended_cache`, `cache_1h`
  don't appear anywhere in the repo.

`setCacheKey` / `promptCacheKey` (`transform.ts:814-847, 912-925`) is
a separate mechanism for providers whose cache is keyed by an opaque
string; opencode uses `sessionID` as the key so the same prompt
coming from the same session hashes the same.

**Compaction invalidates this prefix.** Compaction rewrites the head
of the history, which changes the hash up through the new cache
breakpoints. `prune` similarly changes `output` strings that sit
before the last-two breakpoints, so it also invalidates.

### 3.13 Cache-preserving shrinks (none)

Grepped the entire repo for Anthropic's server-side context-management
controls:

```
$ grep -r 'cache_edits\|context_management\|cacheEdits\|contextManagement' packages/
(no matches)
```

opencode does not use Anthropic's `cache_edits` beta, the
`context_management` body param, or OpenAI's equivalents. All context
shrinking is done **client-side**, with the consequence that any
shrink invalidates the prompt-cache prefix. This is a notable
difference vs Claude Code.

### 3.14 Pre-wire transformations

`ProviderTransform.message` (`provider/transform.ts:305-349`) runs
inside the `wrapLanguageModel` middleware hook
(`session/llm.ts:387-400`). It's the last step before the SDK serialises
to HTTP. It:

- Rewrites empty text / reasoning parts out of Anthropic/Bedrock
  requests (`transform.ts:55-73`).
- Scrubs tool-call IDs for Claude (`transform.ts:75-102`, regex
  `[^a-zA-Z0-9_-]` -> `_`) and Mistral/Devstral (`transform.ts:128-176`,
  first 9 alphanumerics).
- For Anthropic, splits malformed assistant turns of shape
  `[tool_use, …, text]` into `[text]` + `[tool_use, …]`
  (`transform.ts:103-127`).
- For Mistral, when a tool message is followed by a user message,
  injects a synthetic `assistant: "Done."` turn to satisfy
  Mistral's ordering rules (`transform.ts:162-173`).
- Filters unsupported modalities on models without that modality
  capability, replacing them with a plain-text error note
  (`transform.ts:267-303`).
- For models with an `interleaved.field` capability, extracts
  `reasoning` parts and moves them into
  `providerOptions.openaiCompatible[field]`
  (`transform.ts:178-210`).

None of these are "context compaction" — they're payload shape fixups.
They do not change logical context length.

### 3.15 Plugin hooks that can rewrite context

Two hooks run on every chat turn:

- `experimental.chat.messages.transform` — arbitrary mutation of the
  `msgs: MessageV2.WithParts[]` array before `toModelMessagesEffect`.
  Called for normal turns (`session/prompt.ts:1471`) **and inside
  compaction** (`session/compaction.ts:303`). Any plugin wiring this
  can compact / rewrite context. No plugin in the repo currently
  implements it.
- `experimental.chat.system.transform` — arbitrary mutation of the
  `system: string[]` array. `session/llm.ts:114-124` also contains a
  tidy-up after this hook: if `system.length > 2` and the first
  entry (the base system prompt) is unchanged, everything from the
  second onward is rejoined into a single string so the 2-part
  layout expected by `applyCaching` is preserved.

### 3.16 `finish-step` snapshot / diff summaries (unrelated to context)

`session/summary.ts` is a **file-diff** summariser, not a
conversation summariser. It walks snapshot boundaries in
`step-start`/`step-finish` parts and computes added/deleted/files
counts per session and per user turn (`summary.ts:82-129`). It does
not modify the message history; it only populates
`session.summary.{additions, deletions, files, diffs}` for display in
the TUI. Easy to confuse with compaction — it's not compaction.

---

## 4. Interaction map — what happens when the context pressure rises

In one tick of `runLoop` (`session/prompt.ts:1305`):

1. **Build the visible set.**
   `MessageV2.filterCompactedEffect(sessionID)` projects the DB into
   the window after the latest completed compaction
   (`message-v2.ts:931-960`). Pre-compaction history is dropped here.
2. **Pending compaction task?** If `tasks.pop()` yields a
   `CompactionPart`, jump to `compaction.process` and return.
3. **Post-turn overflow check.** If the last assistant reply was a
   real turn (`lastFinished`, not a summary) and
   `compaction.isOverflow({ tokens, model })` is true
   (`session/prompt.ts:1384-1391`):
   - `compaction.create({ auto: true })` appends a new user message
     with a CompactionPart and **continues** (back to step 1).
4. **Serialise for the model.**
   `toModelMessagesEffect(msgs, model)` converts to `ModelMessage[]`,
   substituting `[Old tool result content cleared]` for any tool
   parts already marked `state.time.compacted` by prior pruning
   (`message-v2.ts:649-840`).
5. **Provider-specific pre-wire fixup.**
   `ProviderTransform.message` inside the AI SDK middleware
   (`llm.ts:387-400`). If Anthropic-family, `applyCaching` adds
   `cacheControl: {type:"ephemeral"}` to 1st two system + last two
   non-system messages (`transform.ts:216-265`).
6. **Stream the call.** If the provider rejects with a context-limit
   error, `fromError` returns `ContextOverflowError`; the processor
   flips `needsCompaction = true` and the step returns `"compact"`.
   The loop then `compaction.create({ auto: true, overflow: true })`
   (`session/prompt.ts:1515-1522`) and continues.
7. **Compaction run.** Next loop tick hits step 2.
   `compaction.process` (`compaction.ts:221-457`):
   1. Optionally pulls the overflow-causing user message aside as
      `replay` and trims the compaction input
      (`compaction.ts:242-258`).
   2. `select` computes `(head, tail_start_id)` using `tail_turns`
      and a 25%-of-usable (clamped [2k, 8k] tokens) recent budget.
      If the last turn alone exceeds the budget, falls back to
      summarising the whole history.
   3. Runs plugin hooks
      `experimental.session.compacting` +
      `experimental.chat.messages.transform`.
   4. Calls the compaction agent with
      `toModelMessagesEffect(msgs, model, { stripMedia: true })` on
      the head — media attachments become
      `"[Attached <mime>: <file>]"`.
   5. On success publishes the summary assistant message, updates the
      CompactionPart's `tail_start_id` if `select` refined it, and if
      `auto`:
      - replays the trimmed user message verbatim (stripped media),
        or
      - asks `experimental.compaction.autocontinue` and, if
        `enabled: true` (default), appends a synthetic continue
        prompt with `metadata.compaction_continue: true`.
   6. On failure, marks the assistant with a `ContextOverflowError`
      and the loop breaks.
8. **Post-loop prune.** After the outer loop exits,
   `compaction.prune({ sessionID })` is forked
   (`session/prompt.ts:1530`). It marks old tool outputs past the
   last 40 k tokens as `time.compacted`, but only if total pruned
   exceeds 20 k tokens. This does not interact with step 4 of the
   current request (it ran after the user-visible output is done),
   but it will take effect on the **next** request.

Fallback order, then:

- **Always running:** per-tool truncation (50 KiB / 2000 lines) +
  per-tool internal caps.
- **First line of context-pressure defense:** `filterCompacted` after
  any earlier compaction.
- **Second line:** auto-compaction triggered by token usage crossing
  `usable(cfg, model)`.
- **Third line:** auto-compaction triggered by the provider returning
  a context-overflow error (media-stripping mode).
- **Background maintenance:** `prune` after each user request.
- **Not present:** idle/time-based compaction, cache_edits / server-
  side context_management, per-turn aggregate output cap.

---

## 5. Constants reference

| Constant | Value | Source | Used for |
|---|---|---|---|
| `MAX_LINES` | `2000` | tool/truncate.ts:15 | per-tool truncation line cap |
| `MAX_BYTES` | `50 * 1024` (50 KiB) | tool/truncate.ts:16 | per-tool truncation byte cap |
| `RETENTION` | `7 days` | tool/truncate.ts:13 | tool-output file retention |
| `DEFAULT_READ_LIMIT` | `2000` | tool/read.ts:15 | Read tool default lines |
| `MAX_LINE_LENGTH` (read) | `2000` | tool/read.ts:16 | Per-line cap in Read |
| `MAX_BYTES` (read) | `50 * 1024` | tool/read.ts:18 | Read tool byte cap |
| `MAX_LINE_LENGTH` (grep) | `2000` | tool/grep.ts:11 | Per-match excerpt cap |
| Grep `limit` | `100` | tool/grep.ts:102 | Grep match count cap |
| `MAX_RESPONSE_SIZE` (webfetch) | `5 * 1024 * 1024` (5 MiB) | tool/webfetch.ts:9 | Webfetch body cap |
| `DEFAULT_TIMEOUT` (webfetch) | `30 * 1000` ms | tool/webfetch.ts:10 | |
| `MAX_TIMEOUT` (webfetch) | `120 * 1000` ms | tool/webfetch.ts:11 | |
| Bash `keep` ring buffer | `Truncate.MAX_BYTES * 2` = 100 KiB | tool/bash.ts:423-425 | bash streaming cap |
| `COMPACTION_BUFFER` | `20_000` tokens | session/overflow.ts:6 | Default `reserved` cap value |
| `PRUNE_MINIMUM` | `20_000` tokens | session/compaction.ts:33 | Min tokens of prune-able output to trigger prune |
| `PRUNE_PROTECT` | `40_000` tokens | session/compaction.ts:34 | Recent tool-output tokens exempt from prune |
| `PRUNE_PROTECTED_TOOLS` | `["skill"]` | session/compaction.ts:35 | Never-pruned tools |
| `DEFAULT_TAIL_TURNS` | `2` | session/compaction.ts:36 | Default `tail_turns` |
| `MIN_PRESERVE_RECENT_TOKENS` | `2_000` | session/compaction.ts:37 | Floor of auto `preserve_recent_tokens` |
| `MAX_PRESERVE_RECENT_TOKENS` | `8_000` | session/compaction.ts:38 | Ceiling of auto `preserve_recent_tokens` |
| Auto `preserve_recent_tokens` formula | `clamp(floor(usable * 0.25), 2k, 8k)` | session/compaction.ts:45-50 | Budget for retained tail |
| `OUTPUT_TOKEN_MAX` | `32_000` (override via env) | provider/transform.ts:20 | Cap on requested `maxOutputTokens` |
| `DOOM_LOOP_THRESHOLD` | `3` | session/processor.ts:24 | Identical tool-call repeats before asking |
| `CHARS_PER_TOKEN` | `4` | util/token.ts:1 | Local token estimator |
| Token-estimator output | `round(length / 4)` | util/token.ts:3-5 | Used by prune, compaction tail sizing |
| Truncate cleanup cadence | hourly, 1-minute delay | tool/truncate.ts:133-134 | Cleanup schedule |
| `RETENTION` on truncate files | 7 days | tool/truncate.ts:13 | |

---

## 6. Feature flags / config

### Config (`packages/opencode/src/config/config.ts:207-226`)

| Key | Type | Default | Effect |
|---|---|---|---|
| `compaction.auto` | boolean | `true` | Disable auto-compaction (`overflow.ts:20`) |
| `compaction.prune` | boolean | `true` | Disable tool-output pruning (`compaction.ts:175`) |
| `compaction.tail_turns` | int ≥ 0 | `2` | # recent user turns to keep verbatim (`compaction.ts:135`) |
| `compaction.preserve_recent_tokens` | int ≥ 0 | auto-clamped 2k–8k | Token budget for the kept tail (`compaction.ts:47`) |
| `compaction.reserved` | int ≥ 0 | `min(20_000, maxOutputTokens)` | Headroom subtracted from `input`/`context` (`overflow.ts:13`) |
| `experimental.batch_tool` | boolean | `false` | Declared (`config.ts:230`) but no code currently reads it. Dead flag as of `d2181e927`. |
| `experimental.continue_loop_on_deny` | boolean | `false` | Affects permission-deny doom-loop behaviour (`processor.ts:542`) — not a context mechanism. |

### Env flags (`packages/opencode/src/flag/flag.ts`)

| Env var | Default | Effect |
|---|---|---|
| `OPENCODE_DISABLE_AUTOCOMPACT` | off | Forces `compaction.auto = false` (`config.ts:686-687`) |
| `OPENCODE_DISABLE_PRUNE` | off | Forces `compaction.prune = false` (`config.ts:689-691`) |
| `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` | unset → 32 000 | Overrides `OUTPUT_TOKEN_MAX` (`transform.ts:20`) |
| `OPENCODE_EXPERIMENTAL` | off | Turns on several experimental features but not the compaction ones |

### Agent

Compaction is itself an agent named `"compaction"`
(`agent/agent.ts:188-202`). It is `hidden: true, mode: "primary"`,
permissions `"*": deny`, so it can't call tools. A user can override
the model used for compaction via
`agents.compaction.model` in config
(`config.ts:176`).

---

## 7. What's actually enabled out of the box

On a fresh install (no config, no env vars):

- **Auto-compaction: ON.** Triggered whenever a `finish-step`
  `tokens.total ≥ usable(model)` or the provider returns a
  context-limit error.
- **Prune: ON.** Runs after every user request, marks tool outputs
  older than the last 40 k tokens of tool output (with a 20 k-token
  minimum payoff) as compacted.
- **Per-tool truncation: ON**, universal. 50 KiB / 2000 lines, hard.
- **Prompt-cache breakpoints: ON** for Anthropic-family,
  OpenRouter, OpenAI-compat, Copilot, Alibaba, Bedrock — ephemeral
  5-min cache only.
- **`promptCacheKey = sessionID`: ON** for OpenAI, Azure, OpenRouter,
  Venice, and opencode-hosted GPT-5.
- **Gateway caching `auto`: ON** when using `@ai-sdk/gateway`.
- **Media stripping on overflow-retry: ON.**
- **`filterCompacted` projection: ON** (always).
- **Agent step cap via max-steps prompt: ON** when an agent sets
  `steps`; most don't, so `agent.steps ?? Infinity`.

Off by default / requires opt-in:

- `experimental.batch_tool` (declared, unused).
- `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` override.
- Plugin `experimental.chat.messages.transform`,
  `experimental.compaction.autocontinue`,
  `experimental.session.compacting` — no plugin in the repo wires
  them.
- Any long (1 h) Anthropic cache TTL — **not implemented.**
- Any Anthropic `context_management` / `cache_edits` betas — **not
  implemented.**

---

## 8. Notable absences

Searched for, not found:

- **Idle / time-based compaction.** Nothing fires based on wall-clock
  elapsed since last turn. The only time-based things are
  `Truncate.cleanup` (7-day sweep of tool-output files, hourly) and
  provider/session HTTP timeouts. `grep -r 'idle|cache.?TTL|cache.*1h'
  packages/opencode/src` returns only unrelated matches (session UI
  state, HTTP idle timeouts, provider ModelsDev cache).
- **Per-turn aggregate output budget.** N parallel tool calls each
  get their own 50 KiB envelope. No cap across a turn.
- **Server-side cache-preserving shrinks.** No use of Anthropic
  `context_management`, `cache_edits`, or equivalent. All shrinking
  invalidates the cache prefix. `grep -r
  'cache_edits|context_management' packages/` is empty.
- **Long (1 h) Anthropic cache.** Only `{type: "ephemeral"}` (5 min)
  is used. No `"1h"` / `cache_1h` anywhere.
- **Local tokeniser.** `util/token.ts` is `chars / 4` only. Real
  token counts come from the provider response and flow through
  `Session.getUsage`.
- **UI-only collapsing that differs from the wire.** The TUI hides
  what is already marked compacted/summarised; the wire sees the
  same thing. No display-only dedup of repeated tool calls.
- **Separate `/compact` code path.** `/compact` in the TUI calls the
  `/session/:id/summarize` HTTP route
  (`cli/cmd/tui/routes/session/index.tsx:495-499`), which ends up
  calling exactly the same `compaction.create({ auto: false })` plus
  `prompt.loop` sequence as auto-compaction
  (`server/routes/instance/session.ts:574-599`). `/compact`
  == autocompact, modulo the `auto` flag that controls the
  auto-continue behaviour.

---

## 9. Interesting findings

- **The compaction "agent" is a real agent with `"*": deny`.** It
  gets the system prompt from `agent/prompt/compaction.txt` layered
  with the per-turn compaction prompt template. Because it shares
  the regular processor pipeline
  (`compaction.ts:333-352`), it also gets plugin hooks,
  provider transforms, and prompt-cache markers. Tools are disabled
  not via "no tools" but via a permission-deny rule — if a plugin
  registers a tool the compaction agent still has its schema sent
  on the wire, but permission-denied at call time
  (`processor.ts:287-331`).

- **`tail_fallback`.** If the single most-recent turn is bigger than
  `preserve_recent_tokens`, the compaction *summarises the whole
  history, no tail* (`compaction.ts:150-153`). This is a potential
  foot-gun: a giant pasted file makes compaction drop everything
  including the very message that caused the overflow. The
  `overflow` branch mitigates this by pulling the overflowing user
  message aside as `replay` and stripping media before replaying
  (`compaction.ts:242-258, 386-398`).

- **Compaction is durable.** The summary becomes a regular assistant
  message with `summary: true, mode: "compaction"` stored in
  session.sql. `SessionRevert` can undo it, so "full compaction
  doesn't lose anything" is literally true — the raw history is
  still in the DB, just not sent to the model. (See
  `session/revert.ts` and `session/revert-compact.test.ts`.)

- **Prune never un-prunes.** Once `state.time.compacted` is set it's
  set forever (`compaction.ts:198` short-circuits on already-pruned
  parts). There's no "if we're well below budget now, put tool
  outputs back." Given `prune` runs after every user request, after
  a long session nearly all non-tail tool outputs become permanent
  `[Old tool result content cleared]` placeholders. The tail kept
  depends only on the running total, not wall-clock staleness.

- **The cache breakpoints' placement is brittle against plugin
  `system.transform`.** `llm.ts:114-124` handles this explicitly: if
  a plugin adds entries and the original first entry is still there,
  the rest are rejoined into a single string so the final `system`
  list is still 1 or 2 entries, matching what `applyCaching` expects.
  This is an intentional "preserve the cache prefix" trick.

- **A dummy `_noop` tool is injected** when a LiteLLM-like proxy
  (or github-copilot) has no active tools but the history contains
  tool calls (`llm.ts:213-228`). This happens in compaction (tools
  are all denied), so opencode explicitly plans for a dummy-tool
  round-trip through those proxies. The tool description tells the
  model not to call it.

- **Overflow replay converts media to text inline**
  (`compaction.ts:386-398`): the replayed user message's file parts
  become `[Attached image/png: foo.png]` etc. so the message shape
  still makes sense — useful when a user's original message is
  "look at this screenshot" and the screenshot is what blew up the
  context.

- **The tail budget auto-scales** with `usable(model) * 0.25`, so
  bigger models get more tail preservation (up to the 8 k cap).
  Small models land at the 2 k floor.

- **`experimental.batch_tool` is declared in config schema but not
  consumed anywhere.** It's either pre-wiring for a future tool or
  dead code; a user setting it to `true` today does nothing.

- **No "cache warming" on idle.** Because the only cache is
  ephemeral (5 min), a session left for >5 minutes always cache-
  misses on next turn. There's no heartbeat or pre-emptive
  refresh. Combined with compaction also invalidating the prefix,
  large-session cache hit rates are inherently upper-bounded.
