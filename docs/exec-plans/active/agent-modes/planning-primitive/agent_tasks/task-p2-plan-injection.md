<!-- AGENT_TASK_START: task-p2-plan-injection.md -->

# Task P2 — Post-Compaction Plan Injection (Planning Primitive)

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — **"How Planning Primitive composes"** (the plan is "injected post-compaction so it survives Track 7"; exactly-one-`in_progress` is **prompt-layer guidance in the preamble**, not tool-layer); **"What stays open … Rendering format for injection (Markdown checkbox vs JSON vs compact list)"** — this task resolves that for v1; and **"Pre-compaction flush hook interaction with Track 7 (tentatively: no — the plan is durable + injected post-compaction)"**.
2. `docs/exec-plans/active/agent-modes/planning-primitive/plan.md` — §A0 point **5** (the injection uses neutral framing, mirroring the Track-7 pre-Tier-3 memory-flush precedent — NOT a "you are being compacted" message), §A4 decision **2** + §A5 row 1 (injection position / KV-cache), §A1.2 (the P2 overview item), §A1.2 + §B row **P2** (output contract), §A5 row "Plan injection busts KV-cache" / §A4 decision 2 ("`agent_node` → plan injection post-hook; empty plan → no injection"), §A5 risk "Plan injection busts KV-cache" (P2 includes a cache-stability-adjacent test: repeat-run byte-identical injected block for an unchanged plan).
3. `services/worker-service/executor/graph.py` — `agent_node` (`:1344`). The injection point is **after** `compaction_pre_model_hook` returns (`:1383`, producing `pass_result.messages`) and **before** the cache markers are applied (`:1404`, `_cache_strategy.apply_cache_markers(pass_result.messages)`). Inject your plan `SystemMessage` into the projected message list in that window so it (a) survives Tier 1/Tier 3 compaction — the hook already ran — and (b) sits at a stable position the cache strategy then marks. Read `:1395`–`:1410` carefully: `messages_for_llm` is derived from `pass_result.messages`; the plan block must be present in the list handed to `apply_cache_markers`.
4. The **Track-7 pre-Tier-3 memory-flush** is the explicit precedent for "inject a neutral system block after compaction." Find it via the `memory_flush` references around the hook / `pre_model_hook.py` and `state.py:206` (`memory_flush_fired_this_task`). Match its neutral framing and placement discipline — do not invent a new "compaction is happening" preamble.
5. `services/worker-service/executor/compaction/state.py` — confirm the `plan` channel shape landed by **P1** (`plan: Annotated[list[dict], _plan_replace_reducer]`, items `{id, title, status}`).

**SHARED-FILE / WORKTREE WARNING:** This task edits `executor/graph.py` (`agent_node`). **The Supervisor Topology track edits `graph.py` heavily** (S8 `_build_graph` branching; S3/S4 `_get_tools`). Per plan.md §A3, any parallel agent touching `graph.py` **uses `isolation: "worktree"`**; merge after. Keep this edit localized to the post-hook/pre-cache-marker window in `agent_node` to minimize merge surface.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests covering the change under the pinned venv (`services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/<your_test_file>.py`). The injection-format + byte-stability tests are pure unit tests over the formatting helper + the post-hook list assembly. If you assert end-to-end across a real compaction, use `make e2e-test PYTEST_ARGS='-k <your_test>'` on the isolated harness — never raw `pytest tests/backend-integration` in a worktree.
2. Update the status of P2 in `docs/exec-plans/active/agent-modes/planning-primitive/progress.md` to "Done".

## Context

P1 stores the plan in the `plan` channel. But Track 7 compaction rewrites the message history — Tier 1 masks tool results, Tier 3 replaces the middle with a summary — so a plan the agent wrote 20 turns ago is not guaranteed to survive into the projected prompt. P2 re-injects the current plan as a neutral `SystemMessage` **after** the compaction hook has produced its projection, so the plan is always present in the prompt regardless of what compaction dropped.

**Cache-position is the load-bearing decision (plan §A4 decision 2 + §A5 row 1).** The byte-identity test alone covers only the *unchanged* plan. But the **normal case for a planning agent is a CHANGING plan** — agents call `plan_write` often (mark an item `in_progress`, then `completed`, add the next item), so the injected block's content changes turn-to-turn. The KV-cache consequence depends entirely on **where** the block sits relative to the cache breakpoints `apply_cache_markers` places (`graph.py:1404`): a block inside the cached *prefix* would, on every plan change, invalidate the cache for **everything after it** (catastrophic — the whole suffix re-prefills each turn). Placed in the **uncached suffix** (after the last cache breakpoint), a plan change invalidates only the **minimum** span — the block is simply re-sent each turn (cheap: a few hundred tokens, bounded by P1's caps), and the expensive prefix cache survives. P2 must **pin the injection position into the uncached suffix** and verify that placement against the real breakpoint logic, not merely assert byte-identity for an unchanged plan.

## Task-Specific Shared Contract

- **Injection point:** in `agent_node`, after `compaction_pre_model_hook` returns `pass_result` (`:1383`) and **before** `_cache_strategy.apply_cache_markers(...)` (`:1404`). The plan block is part of the list passed to `apply_cache_markers` / used as `messages_for_llm`. It is NOT persisted into `state["messages"]` (the journal) — it is a per-call projection addendum, exactly like the compaction hook's assembled projection. (The durable source of truth is the `plan` channel from P1; the journal is never mutated by injection.)
- **Injection POSITION — pin to the uncached suffix (plan §A4 decision 2 + §A5 row 1 — load-bearing):** the plan content **changes most turns** (planning agents call `plan_write` frequently), so the block must sit where a change invalidates the **minimum** cache span. **Verify against the real breakpoint logic before choosing a slot:** the Anthropic strategy (`executor/prompt_cache/anthropic.py:119`) marks **two** breakpoints — the **last `SystemMessage`** and the **tail message**. Therefore: do **NOT** let the plan block become the marked last-`SystemMessage` (it would put a cache breakpoint on changing content and bust the prefix up to it on every plan edit). Place the plan block **after** the last *stable* system message — in the uncached suffix, after the final cache breakpoint — so a plan change re-prefills only the already-uncached tail. Accept that the block is **re-sent each turn** (cheap — a few hundred tokens, bounded by P1's caps); that is strictly preferable to a prefix-busting position. Confirm the chosen index produces this against `apply_cache_markers`'s actual marking (and the noop/openai/bedrock strategies — none should end up marking the plan block as a stable-prefix breakpoint).
- **Empty plan → no injection.** When `state.get("plan")` is empty/absent, inject nothing (no empty `SystemMessage`, no placeholder). This keeps the prompt byte-identical to the pre-Planning shape for agents that never call `plan_write`, so existing cache behavior is untouched.
- **Neutral framing (§A0 point 5).** The injected block is a plain "current plan" reminder — e.g. a short preamble line + the rendered checklist. It must NOT mention compaction, summarization, or "you are being compacted." Match the tone/placement of the Track-7 memory-flush block.
- **Injection FORMAT — RESOLVED for v1 (the open design question):** **Markdown checkbox checklist** — render each item as a checkbox line keyed on status (`completed` → checked box, `pending`/`in_progress` → unchecked, with `in_progress` distinguished by an inline marker such as `(in progress)`). Document this as the resolution of the design's "Rendering format for injection (Markdown checkbox vs JSON vs compact list)" open item, with the rationale: checkbox markdown is the format the model is most fluent in (matches Claude Code's `TodoWrite` rendering), is compact (token-budget friendly), and is human-legible in replay logs. Implement the rendering in a small pure helper (`render_plan_block(plan_items) -> str`) so P2's tests and P4's Console parity reasoning share one canonical format reference.
- **One-`in_progress` guidance lives in the preamble text (prompt-layer).** The injected preamble carries the soft instruction (e.g. "Keep exactly one item `in_progress`."). This is the *only* place that rule is expressed — the P1 tool does not enforce it. Keep the preamble text a stable constant (deterministic, no per-turn variation) so the only thing that ever changes in the block is the checklist body. (Note: the block sits in the **uncached suffix** per the injection-position rule above — the stable-constant preamble is about determinism, not about living in the cached prefix.)
- **Cache stability — two complementary properties:** (1) for an *unchanged* plan, `render_plan_block` must be **byte-identical** across repeat calls (deterministic ordering, no timestamps, no nondeterministic dict iteration) — so an unchanged plan changes nothing; and (2) for a *changed* plan (the normal case), the changing block lives in the **uncached suffix** (injection-position rule) so the change re-prefills only the tail, never the cached prefix. The byte-identity test is necessary but **not sufficient** — it does not exercise the changing-plan / breakpoint-position hazard, which §A4 decision 2 + §A5 row 1 require a separate position assertion to cover.
- **Token budget cap:** honor P1's item/title caps; if the rendered block would still be very large, it is bounded by P1's 50-item / 200-char caps — P2 does not add a second truncation layer (no silent truncation). (This closes the "injection token budget" sub-part of the design's size-limits open item: the budget is bounded upstream by P1's caps.)

## Affected Component

- **Service/Module:** Worker Service — `agent_node` projection assembly
- **File paths:**
  - `services/worker-service/executor/graph.py` (modify — `agent_node`, post-hook/pre-cache-marker injection)
  - `services/worker-service/tools/plan_tools.py` **or** a new `executor/plan_injection.py` (add `render_plan_block` + the preamble constant — co-locate with P1's plan code if cohesive, else a small new module; keep it import-light so `agent_node` stays thin)
  - `services/worker-service/tests/test_plan_injection.py` (new — format + byte-stability + empty-plan + neutral-framing assertions)
- **Change type:** new pure rendering helper + a localized `agent_node` injection

## Dependencies

- **Must complete first:** **P1** (the `plan` channel + item shape). P2 reads `state["plan"]`.
- **Provides output to:** **P5** (integration test asserts the injected block is present post-compaction). Not a hard dependency for P3/P4 (those project/render the channel independently), but P4 should reference `render_plan_block` as the canonical format so Console rendering matches the injected format.
- **Shared interfaces/contracts:** the `render_plan_block` format + the preamble constant (canonical plan rendering, reused conceptually by P4).
- **Worktree note:** shared `graph.py` with the Supervisor Topology track — see SHARED-FILE warning.

## Implementation Specification

### New helper: `render_plan_block(plan_items) -> str`
- Deterministic Markdown checkbox rendering; stable item ordering (preserve the stored list order — P1 writes verbatim, so order is the agent's intent); status → box state mapping; `in_progress` inline marker. Empty input → returns empty string (caller treats empty as "no injection").
- A module-level preamble constant carrying the neutral framing + the one-`in_progress` soft guidance.

### Modify: `agent_node` (graph.py, ~`:1383`–`:1404`)
- After `pass_result` is obtained and before cache markers are applied: compute the plan block from `state.get("plan")`. If non-empty, build the plan message (`preamble + "\n" + block`) and insert it at a **deterministic position in the uncached suffix** per the injection-position rule (§A4 decision 2) — i.e. **not** where it would become `apply_cache_markers`'s last-`SystemMessage` breakpoint. Inserting it **after** the last stable system message (toward the tail, ahead of / at the most-recent user-or-tail content) keeps a plan change off the cached prefix. The list that reaches `apply_cache_markers` (and `messages_for_llm` in the no-marker branch) includes it. **Verify the resulting marker placement** (the plan block must not carry the stable-prefix breakpoint) rather than assuming a slot is safe.
- Do not mutate `state["messages"]`. Do not change the compaction hook's contract.

## Acceptance Criteria

- [ ] With a non-empty plan, the LLM-bound message list (the one passed to `apply_cache_markers`) contains a `SystemMessage` whose body is the neutral preamble + the markdown checklist.
- [ ] With an empty/absent plan, **no** plan `SystemMessage` is injected (message list byte-identical to pre-Planning shape).
- [ ] The injected block survives compaction: the injection happens after `compaction_pre_model_hook` returns, so a Tier-1/Tier-3 projection still carries the plan block (covered end-to-end by P5; P2 asserts the ordering at the `agent_node` seam).
- [ ] `render_plan_block` is byte-identical across repeated calls for an unchanged plan (cache-stability test).
- [ ] **Injection position is in the uncached suffix (§A4 decision 2):** after `apply_cache_markers` runs on a projection containing the injected plan block, the plan block does **not** carry the stable last-`SystemMessage` cache breakpoint (it sits after it, in the tail/suffix). A test asserts the breakpoint placement relative to the injected block — **not** merely unchanged-plan byte-identity — so a *changed* plan invalidates only the minimum (already-uncached) suffix. Cross-ref §A4 decision 2 + §A5 row 1 and the repo's cache-stability concern (Track 7 / CLAUDE.md).
- [ ] The injected text contains the one-`in_progress` soft guidance and contains NO compaction/summarization language (neutral-framing assertion).
- [ ] `state["messages"]` (the durable journal) is unchanged by injection.
- [ ] Narrowest-scope tests pass under the pinned venv.

## Testing Requirements

- **Unit (no infra):** `render_plan_block` format for each status; empty → `""`; **byte-identical repeat-run** for an unchanged plan (the cache-stability-adjacent test §A5 requires); neutral-framing assertion (no banned substrings like "compact"/"summariz"); preamble carries the one-`in_progress` guidance.
- **`agent_node` seam:** with a fake hook result + a populated `plan`, assert the plan block is present in the list handed to the cache strategy and absent when the plan is empty; assert `state["messages"]` untouched.
- **Cache-position (§A4 decision 2):** run `apply_cache_markers` over a projection that includes the injected plan block and assert the block is **not** the breakpoint-marked last-`SystemMessage` — i.e. it lands in the uncached suffix (after the final stable cache breakpoint). A complementary assertion: editing the plan (changing-plan case) leaves the marked prefix identical, only the suffix differs. Verify against the real Anthropic strategy (`executor/prompt_cache/anthropic.py`), not a stub.
- Run the narrowest scope only.

## Constraints and Guardrails

- **Inject post-compaction only** — after `compaction_pre_model_hook` (`:1383`), before cache markers (`:1404`). Never before the hook (it would be compacted away).
- **Neutral framing** — no "you are being compacted" language (§A0 point 5).
- **Empty plan → no-op** — never inject an empty/placeholder block.
- **Byte-stable rendering** — deterministic order, no timestamps/nondeterminism (an unchanged plan must not perturb anything).
- **Position in the uncached suffix (§A4 decision 2)** — the (frequently-changing) plan block must NOT carry the stable-prefix cache breakpoint; place it after the last stable system message so a plan change re-prefills only the tail. Verify against `apply_cache_markers`'s real two-breakpoint marking, not an assumption.
- Do not enforce one-`in_progress` programmatically — it is preamble guidance only.
- Do not mutate `state["messages"]` — injection is projection-only.
- Do NOT add a pre-compaction flush hook for the plan — the design tentatively decided against it (plan is durable + injected post-compaction).
- Worktree-isolate the `graph.py` edit against the Supervisor Topology track.

## Assumptions

- P1 has landed the `plan` channel + item shape; `state["plan"]` is a `list[dict]` of `{id, title, status}`.
- The `:1383`/`:1404` anchors hold; if `agent_node` shifted, locate by the `compaction_pre_model_hook(...)` call and the `_cache_strategy.apply_cache_markers(...)` call and inject between them.
- The cache strategy marks **two** breakpoints (last `SystemMessage` + tail message — `executor/prompt_cache/anthropic.py:119`); the injected plan block must sit in the uncached suffix so its frequent changes don't bust the stable prefix (§A4 decision 2). Do not assume "any extra `SystemMessage` is harmless" — a plan block that becomes the last-`SystemMessage` breakpoint would re-prefill the prefix on every plan edit.

<!-- AGENT_TASK_END: task-p2-plan-injection.md -->
