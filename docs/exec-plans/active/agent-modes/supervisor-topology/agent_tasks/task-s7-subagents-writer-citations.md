<!-- AGENT_TASK_START: task-s7-subagents-writer-citations.md -->

# Task S7 — Subagent Findings Contract + One-Shot Writer + Citation Binding

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-modes/design.md` — sections "Topology 2: Supervisor" (the Subagents and one-shot Writer boxes), "Pattern provenance" (the one-shot Writer row — LangChain's hard-learned *"restrict multi-agent to research, and write the report in one-shot"*), "What the Supervisor topology owns" (Subagent + Writer prompt templates; the **citation verification pass** as *"a thin, single-purpose node"*), "Citation binding" (the three-step deterministic binding + the **load-bearing immutability invariant** — findings are *"immutable and addressable by `finding_id`"*, quotes never mutated), "What customers configure" (`writer_style`: formal report vs. annotated bullets), and the "Open decisions" section (the **one-shot Writer reduction *algorithm*** — S7 owns the v1).
2. `docs/exec-plans/active/agent-modes/supervisor-topology/plan.md` — §A0 (invariant 4 *findings immutable, addressable by `finding_id`*, invariant 7 *depth cap 2 / sub-agents are ReAct-only*), §A4.1 (**S7** row), §A5 (Writer → `citations.resolve`/`citations.verify`; unresolved id → render error flag; unsupported quote → verify flag, **not** a fabricated source), §A7 (the `subagent_finding` event; "Caps surfaced — when the Writer's finding corpus is reduced, `log()` what was dropped"), and §A8 ("One-shot Writer context overflow" → **S7 owns the v1 reduction + the open flag**).
3. `task-s6-supervisor-fanout-iteration.md` (S6) — the `subagent_results` reducer keyed by `subtask`, the `findings` accumulation, and the iteration `stop` edge that routes to the Writer. S7's `subagent_node` runs inside the `Send` fan-out S6 wired; S7's `writer_node` is the stop target.
4. `task-s5-supervisor-scope.md` (S5) — `supervisor/state.py` (`findings`, `brief`) and `supervisor/prompts.py` (S5 added the Scope template; **S7 owns the Supervisor/Subagent/Writer templates** — coordinate so additions are non-conflicting).
5. `task-s3-shared-fanout-helper.md` (S3) — `run_subagent`'s `SubagentResult` shape; the `subagent_node` produces the structured finding(s) `run_subagent` returns.
6. `services/worker-service/executor/graph.py` — the existing ReAct nodes as the async-node + LLM-access **pattern**; and how the repo does structured output (for the findings + Writer citation parsing). S7 mirrors conventions; it does not edit this file.

**CRITICAL POST-WORK:** After completing this task:
1. Run the narrowest worker tests through the pinned venv / isolated harness: `make e2e-test PYTEST_ARGS='-k writer or citation or subagent_finding'`. Unit layer: `services/worker-service/.venv/bin/python -m pytest services/worker-service/tests/test_supervisor_citations.py`. Fix regressions.
2. Update `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` — mark S7 "Done".

## Context

S7 closes the Deep Research graph: the **Subagent findings contract**, the **one-shot Writer**, and **citation binding**.

Citations are bound **deterministically**, not left to the Writer's discretion (the design's deliberate divergence from Anthropic's CitationAgent, which still lets the model *choose* the source and so leaves misattribution possible). Three steps:
1. Subagents emit **structured findings**, each `{finding_id, claim, source_url, supporting_quote}` — not freeform prose with inline links.
2. The **Writer cites by `finding_id` only** — it cannot invent a source. An id either resolves to a real finding or it doesn't. The runtime resolves ids → full citations **at render time, with no LLM involvement**.
3. A **thin verification pass** takes each cited sentence + its referenced finding's quote and confirms the quote supports the sentence, flagging the ones that don't (catching the subtler "real source, wrong claim"). One small single-purpose LLM node — **not** a separate topology, and it never fabricates a source.

**Invariant (load-bearing):** findings are immutable and addressable by `finding_id`. The Writer-context **reduction** (an open algorithm S7 picks a v1 for) may drop / reorder / summarize-for-selection — but must **never mutate** a finding's `supporting_quote`, or both the id resolution and the verify pass break.

## Task-Specific Shared Contract

- **Subagent findings contract.** `supervisor/nodes.py::subagent_node` (the node `run_subagent` drives, or the post-processing of its result) emits structured findings, each:
  - `finding_id: str` — stable, unique, addressable. The Writer references it; `citations.resolve` resolves it.
  - `claim: str` — the asserted statement.
  - `source_url: str` — the source.
  - `supporting_quote: str` — **IMMUTABLE** (§A0 invariant 4). Once emitted, no node — including the reduction step — may rewrite it.
  - Each finding emits a `subagent_finding` event `{iteration, subtask, finding_id, source_url}` (claim/quote ride the Langfuse span, not the row, to bound size — §A7). S7 calls the S9-owned emit helper.
- **`supervisor/citations.py`** — two pure-ish functions, no fabrication:
  - `resolve(finding_id, findings) -> citation` — deterministic lookup, **no LLM**. An unresolved id is surfaced as a **render error flag**, never a fabricated source (§A5).
  - `verify(report, findings) -> flags` — a thin pass confirming each cited quote supports its sentence. It **flags** unsupported citations; it does **not** rewrite the report or invent sources (§"Citation binding"). This is the one small LLM node.
- **One-shot `writer_node`.** A single Writer call producing the final report, citing **by `finding_id` ONLY**. Respects `writer_style` (`formal_report` | `annotated_bullets`) from `agent_config.supervisor`. The Writer never emits inline source URLs — only `finding_id` references that the runtime resolves at render.
- **Writer-context REDUCTION ALGORITHM (S7 owns the v1 — the design's open decision).** Many iterations × many subagents × findings funnel into one Writer call; the corpus can exceed the Writer's context. **Pick a v1: a hard cap on the number of findings passed into the Writer.** Selection/reordering only — **never mutate `supporting_quote`** (immutability preserved → id resolution + verify still work). When findings are dropped, **`log()` what was dropped** (no silent truncation — §A7). **Document the reduction as an open flag** (a clearly-commented `WRITER_FINDINGS_CAP` constant + a note pointing to §"Open decisions" that map-reduce summarization / Track-7 compaction-on-Writer-input are the deferred alternatives). The v1 is select-the-top-N (e.g., by recency/iteration or Supervisor relevance order) — pick one ordering and document it; do not build map-reduce summarization here.
- **`supervisor/prompts.py`** holds the Scope (S5) / Supervisor (S6) / **Subagent + Writer (S7)** prompt templates. S7 adds the Subagent template (structured-findings emission) and the Writer template (cite-by-`finding_id`, honor `writer_style`).

## Affected Component

- **Service/Module:** Worker Service — Supervisor topology (Subagent + Writer nodes, citation binding)
- **File paths:**
  - `services/worker-service/executor/supervisor/nodes.py` (modify — add `subagent_node` + `writer_node`; S5/S6 added the earlier nodes)
  - `services/worker-service/executor/supervisor/citations.py` (new — `resolve` + `verify`)
  - `services/worker-service/executor/supervisor/prompts.py` (modify — add Subagent + Writer templates; additive with S5/S6)
  - `services/worker-service/executor/supervisor/state.py` (modify only if the Writer output / verify flags need a declared channel — otherwise leave to S5's superset)
  - `services/worker-service/tests/test_supervisor_citations.py` (new)
- **Change type:** new nodes + new citation module + prompt templates + v1 reduction algorithm

## Dependencies

- **Must complete first:** **S3** (the helper the subagent runs in), **S5** (state superset + `findings` channel + `prompts.py`), **S6** (the `subagent_results` reducer + the iteration `stop` edge that routes to the Writer). S5 → S6 → S7 **serialize** (shared `nodes.py`/`state.py`/`prompts.py`); worktree-isolate if parallel.
- **Provides output to:** **S8** (compiles the full graph including the Writer terminal + verify node), **S9** (the `subagent_finding` event S7 emits; the Console renders the bound citations + verify flags).
- **Shared interfaces/contracts:** the `{finding_id, claim, source_url, supporting_quote}` finding shape (immutable quote); `citations.resolve`/`citations.verify` signatures; the cite-by-`finding_id`-only Writer contract; `WRITER_FINDINGS_CAP` (the v1 reduction flag).

## Implementation Specification

### `subagent_node` — structured findings

The node (post-processing `run_subagent`'s result, or the in-fan-out node body) parses the sub-agent's work into one or more `{finding_id, claim, source_url, supporting_quote}` findings using the repo's structured-output convention. Generate `finding_id`s that are stable + unique within the run. Emit a `subagent_finding` event per finding via the S9-owned helper. Findings flow into `state["findings"]` (the channel S5 declared, S6 accumulates).

### `supervisor/citations.py`

- `resolve(finding_id, findings)`: deterministic dict/index lookup → citation (source_url + quote). No LLM. Missing id → a structured **render error flag** (e.g. a sentinel the Writer-render surfaces), never a fabricated citation.
- `verify(report, findings)`: for each cited sentence, fetch the referenced finding's quote and ask the verify LLM "does this quote support this sentence?" Return per-citation flags. Never rewrite the report; never invent a source.

### `writer_node` — one-shot

A single Writer LLM call over the (reduced) finding corpus + the `brief`. Output cites by `finding_id` only and honors `writer_style`. After the Writer, run `verify`; attach the flags to the result for downstream rendering. Then `resolve` each `finding_id` to a full citation at render.

### v1 Writer-context reduction

Before the Writer call, if `len(findings) > WRITER_FINDINGS_CAP`, select the top-`WRITER_FINDINGS_CAP` by the chosen ordering (document which), drop the rest, and `log()` the dropped findings (ids + count). **Never** mutate `supporting_quote`. Comment the constant as the open-decision flag with a pointer to design §"Open decisions" (map-reduce / Track-7-on-Writer-input deferred).

## Acceptance Criteria

- [ ] `subagent_node` emits structured `{finding_id, claim, source_url, supporting_quote}` findings; each emits a `subagent_finding` event `{iteration, subtask, finding_id, source_url}` via the S9 helper.
- [ ] `supporting_quote` is never mutated by any S7 path — including the reduction step (immutability invariant; verified by a test that reduces a corpus and asserts surviving quotes are byte-identical to the inputs).
- [ ] `writer_node` produces a one-shot report citing by `finding_id` only — no inline source URLs in the model output.
- [ ] `citations.resolve` resolves a valid `finding_id` → citation with no LLM; an unknown id surfaces a **render error flag** (not a fabricated source).
- [ ] `citations.verify` flags a cited sentence whose finding quote does NOT support it, and passes one that does — verified with a fake verify model; it never rewrites the report or invents a source.
- [ ] `writer_style=formal_report` vs. `annotated_bullets` changes the Writer output shape per the template.
- [ ] When `len(findings) > WRITER_FINDINGS_CAP`, the corpus is reduced (select/reorder only), dropped findings are `log()`ed, and the surviving findings' quotes are unchanged.
- [ ] `WRITER_FINDINGS_CAP` is a clearly-commented open-decision flag pointing to design §"Open decisions".
- [ ] Narrowest worker tests pass via the isolated harness; `progress.md` marks S7 Done.

## Testing Requirements

- **Unit tests** (fake models): subagent findings parse to the structured shape; `resolve` hits + miss (render error flag, no fabrication); `verify` flags unsupported + passes supported; Writer cites by id only; `writer_style` switch; reduction drops past the cap + logs + leaves quotes immutable.
- **Immutability test:** feed a corpus > cap through reduction; assert every surviving `supporting_quote` is identical to its input and no quote string was rewritten anywhere in the S7 path.
- **Worktree-concurrency-safe:** ephemeral ports for anything binding a socket.
- **Run narrowest scope:** `make e2e-test PYTEST_ARGS='-k citation or writer'`; direct `pytest` for the unit layer. Do not run full `make test` unless the change reaches beyond `executor/supervisor/`.

## Constraints and Guardrails

- **NO Pattern B.** No sub-agent task rows, no `parent_task_id`, no per-sub-agent leases, no `sub_agent_id` ledger column, no `waiting_for_subagent` state (§A0 invariant 1). Subagents are in-graph (S3/S6).
- **NO `agent_config.mode` field.** `writer_style` and caps come from `agent_config.supervisor` (S1).
- **Findings quotes are IMMUTABLE** (§A0 invariant 4) — the reduction selects/reorders/summarizes-for-selection but NEVER rewrites `supporting_quote`. This is the core S7 invariant.
- **Citation binding never fabricates a source** — `resolve` is deterministic (no LLM); `verify` flags, never invents; the Writer cites `finding_id` only (§A5).
- **One-shot Writer** — a single Writer call; do NOT reintroduce parallel section-writing (the design's load-bearing reason for one-shot).
- **Do not switch durability to async** — any test graph invokes `durability="sync"` (`graph.py:3127`).
- **No refund path / no new ledger exemption** (budget defers to Track 3 — §A0 invariant 5).
- Do not own the `task_events` migration / `ActivityProjectionService` — S9. S7 only calls the emit helper.
- v1 reduction is a **hard cap + log**; do NOT build map-reduce summarization or Track-7-on-Writer-input here (deferred — §"Open decisions").

## Assumptions

- S6 routes the iteration `stop` decision to `writer_node` and has accumulated `findings` via the `subagent_results` reducer.
- S3's `run_subagent` returns enough structure for `subagent_node` to extract findings (or the sub-agent prompt instructs the structured-findings emission directly).
- The repo's structured-output convention (used in S5's clarity assessment / S6's subtask parsing) is reusable for findings + Writer citation parsing.
- `agent_config.supervisor.writer_style` ∈ {`formal_report`, `annotated_bullets`} is bounds-validated by S1.
- A verify LLM model is available via the same model-access path the other supervisor nodes use.

<!-- AGENT_TASK_END: task-s7-subagents-writer-citations.md -->
