<!-- AGENT_TASK_START: task-s10-console-supervisor.md -->

# Task S10 — Console: Preset Selector (locked on edit) + Supervisor Config Section + Sub-agent Activity Tree

**Stream:** S (Supervisor topology) · **Plan:** [`../plan.md`](../plan.md) (S10, §A1.10, §A4.1, §A7, §A9) · **Design:** [`../../../../../design-docs/agent-modes/design.md`](../../../../../design-docs/agent-modes/design.md)

## Agent Instructions

**CRITICAL PRE-WORK (read before writing any code):**
1. [`../plan.md`](../plan.md) — §A0 invariant **#2** (topology is immutable after creation → rendered read-only on edit), §A1.10 (Console scope), §A4 / §A4.1 (S1 config contract: `topology` / `preset` / `supervisor` sub-object field names + bounds; S9 event-shape contract), §A7 (the sub-agent event payloads), §A9 (Console gate — **subagent ships code + `make console-test` + scenario TEXT only; the orchestrator runs Playwright serially after merge**).
2. [`../../../../../design-docs/agent-modes/design.md`](../../../../../design-docs/agent-modes/design.md) — **"Two-layer naming and the config model"** (customer sees **"Deep Research"**, internal `topology = "supervisor"`; preset is the customer-facing selector, topology is immutable), **"What customers configure"** (the **five** supervisor sub-object fields — `max_fanout_per_iteration`, `max_iterations`, `source_allowlist`, `writer_style`, `scope_clarification_enabled`; **budget is the agent-level Track-3 field, NOT in the `supervisor` sub-object**), **"Observability: one task, sub-agent sub-steps"** (round → sub-agent → steps expandable tree, `iteration` + `subtask` markers, *no task-list explosion*).
3. **[`../../../CONSOLE_TASK_CHECKLIST.md`](../../../../../CONSOLE_TASK_CHECKLIST.md)** — the per-task merge gate. Copy its checklist into the Acceptance Criteria of *this* spec (done below) and obey it.
4. **[`../../../CONSOLE_BROWSER_TESTING.md`](../../../../../CONSOLE_BROWSER_TESTING.md)** — §Scenario Authoring Rules, §Agent-Config Coverage Matrix, §Scenario Templates (B + D), §When to Run Which Scenarios, the `data-testid` convention. This is the canonical authoring source.
5. **Pattern files to mirror (cite as you build, do not paste):**
   - `services/console/src/features/agents/ContextManagementSection.tsx` — the **section-component pattern**: `interface Props { value; onChange; ... }`, native `<select>` (`:133`), chip input with `data-chip` + Enter-to-add (`:169-212`), checkbox (`:231-237`), `data-testid` on every interactive element. `SupervisorConfigSection` mirrors this shape.
   - `services/console/src/features/agents/CreateAgentDialog.tsx` — react-hook-form + zod section integration; sandbox/memory/context-management are wired at `:11-12` (import), `:36-43` (zod fields), `:106-107` (`form.watch`), `:139-175` (payload assembly — only-send-what-was-set). Add the preset selector + supervisor fields the same way.
   - `services/console/src/features/agents/AgentDetailPage.tsx` — read/edit duality: `isEditing` state (`:68`), read-only view (`:364` branch), edit form (`:524`+), `<ContextManagementSection>` mounted at `:848`. This is where preset/topology render **read-only / `disabled`** on edit.
   - `services/console/src/features/task-detail/ActivityPane.tsx` — discriminated-union row renderers keyed on `event.kind` (`UserTurnRow :216`, `AssistantTurnRow :247`, `ToolTurnRow :409`, `CompactionMarkerRow :511`), the reusable **`Fold`** component (`:126-168`, `aria-expanded`, children always rendered so inner `data-testid`s stay queryable), `DetailsAffordance` (`:173`, `activity-row-<i>-expand` / `-details`), `data-testid={`activity-row-${index}`}` + `data-kind={event.kind}` on every row.
   - `services/console/src/api/client.ts` — `listActivity` (`:249`) already returns the merged stream; the new `marker.subagent.*` / `marker.supervisor.iteration` events arrive **through the existing endpoint** — no new client method needed (S9 projects them into `ActivityListResponse`).
   - `services/console/src/types/index.ts` — `AgentConfig` (`:274`), `ContextManagementConfig` (`:260`) as the sub-object-type precedent; `ActivityEvent` (`:475`) + `ActivityEventKind` (`:445`) + `ActivityToolCall` (`:463`) — extend the kind union for the new markers.

**CRITICAL POST-WORK:**
1. Run `make console-test` — React unit tests (the narrowest scope covering this change). Must be green.
2. Update this task's status in `docs/exec-plans/active/agent-modes/supervisor-topology/progress.md` (create the row if the file/section doesn't yet exist).
3. **Hand browser verification to the orchestrator.** Do **NOT** call `make start` / `make stop` or any Playwright MCP tool. Ship the new/extended scenario **text** in `CONSOLE_BROWSER_TESTING.md`; the orchestrator runs it serially after merge and records the scenario numbers on the merge commit (AGENTS.md §Browser verification is the orchestrator's job).

## Context

S10 is the **Console surface for the Supervisor topology** ("Deep Research"). It has three pieces, all read-only-consuming the contracts S1 (API config) and S9 (event shapes) already defined — this task adds **no** new provider/topology branching beyond rendering pre-shaped data:

1. **Preset selector at agent creation.** The Create dialog gains a preset dropdown (`chat` | `coding` | `investigation` | `research`). The customer never sets `topology` directly — selecting the `research` preset is how they get the Supervisor shape (design *Two-layer naming*). Selecting `research` reveals the `SupervisorConfigSection`. The internal `topology` is a derived/hidden value, never a customer-facing enum.
2. **`SupervisorConfigSection`** — a new section component (mirroring `ContextManagementSection`) exposing the **five** supervisor sub-object fields the design's *What customers configure* lists (`max_fanout_per_iteration`, `max_iterations`, `source_allowlist`, `writer_style`, `scope_clarification_enabled` — budget is agent-level Track-3, not here), shown only when the selected preset resolves to `topology = supervisor`.
3. **Sub-agent activity tree** in `ActivityPane` — the new `marker.subagent.*` / `marker.supervisor.iteration` events render as an **expandable tree grouped by `iteration` round → sub-agent (`subtask`) → steps**, using the existing `Fold` pattern. One task, sub-steps — *not* new rows in the task list (design *Observability*).

**Invariant #2 (topology immutable):** on `AgentDetailPage` edit mode, the preset/topology is rendered **read-only / `disabled`**, never an editable control. Changing shape means creating a new agent (design *Two-layer naming*; plan §A0 #2). All other supervisor knobs may stay editable per Track 1/3 — but **this task renders the supervisor sub-object read-only on edit as well** unless S1's contract explicitly allows mutating them; default to read-only and confirm against the S1 spec before exposing edit controls. (The conservative read-only-on-edit posture cannot violate the invariant; an over-permissive editable control can.)

## Task-Specific Shared Contract

### 1. Preset selector (Create dialog + read-only on Detail)

- **Create dialog (`CreateAgentDialog.tsx`):** native `<select>` listing the four presets, `data-testid="agent-config-preset"`. Options: `chat` (default), `coding`, `investigation`, `research`. The `research` option's visible label is **"Deep Research"** (customer-facing name); the others use their plain names. Selecting `research` (a) reveals `SupervisorConfigSection`, (b) causes the submit payload to carry `agent_config.preset = "research"` (and S1/S2 derive `topology = "supervisor"` server-side — the Console does **not** synthesize a `topology` field unless S1's contract requires the client to send it; confirm against S1 and prefer sending only `preset`).
- **Derived topology display:** render the resolved internal topology as a small read-only label with `data-testid="agent-config-topology"` (e.g. "Topology: Supervisor" when `research` is selected, "ReAct" otherwise). This makes the two-layer mapping observable and is the element the Detail page re-renders read-only.
- **Agent Detail (`AgentDetailPage.tsx`):** in **both** read-only view and edit mode, the preset and topology render as **non-editable** — `data-testid="agent-config-preset"` and `data-testid="agent-config-topology"` present but `disabled` (edit mode) or plain text (read-only mode). No `<select>` change handler is wired on the Detail edit form for preset/topology. A short note explains "Topology is fixed at agent creation; create a new agent to change it." This is the literal rendering of invariant #2.

### 2. `SupervisorConfigSection.tsx` (new component)

Lives at `services/console/src/features/agents/SupervisorConfigSection.tsx`. Mirror `ContextManagementSection.tsx`'s `Props` shape (`value`, `onChange`, `errors?`) and styling. Rendered **only when** the resolved topology is `supervisor` (i.e. preset `research`). Fields, in order, each with a stable `data-testid`:

| Field | Control | `data-testid` | Bounds (from plan §A4 / S1) |
|---|---|---|---|
| `max_fanout_per_iteration` | number input | `supervisor-config-max-fanout` | `[1, 20]`, default 5 |
| `max_iterations` | number input | `supervisor-config-max-iterations` | `[1, 10]` |
| `source_allowlist` | chip input (mirror the `exclude_tools` chip control) | `supervisor-config-source-allowlist` | ≤ 50 entries; inline error "Maximum 50 entries" on the 51st |
| `writer_style` | native `<select>` | `supervisor-config-writer-style` | enum `{formal_report, annotated_bullets}` |
| `scope_clarification_enabled` | checkbox | `supervisor-config-scope-clarification` | boolean |

- Section header copy briefly frames it customer-facing: "Deep Research configuration — how the agent scopes, fans out, and writes." Do **not** leak the internal word "Supervisor" into customer-visible copy (design *Two-layer naming*: internal term only).
- Payload nesting: the section's values serialize under `agent_config.supervisor` (sub-object), matching S1's field names verbatim. Only-send-what-was-set: an unset section sends no `supervisor` key (mirror `ContextManagementSection`'s `ctxMgmtDirty` pattern at `CreateAgentDialog.tsx:160`).
- The writer_style `<select>` option labels are customer-friendly ("Formal report" / "Annotated bullets") with the enum values as option `value`s.
- **No provider/topology branching beyond the show/hide gate.** The section renders the same regardless of model/provider; it is purely the pre-shaped `supervisor` sub-object.

### 3. Sub-agent activity tree (`ActivityPane.tsx`)

S9 projects four worker event types into `ActivityListResponse` events. Extend `ActivityEventKind` (`types/index.ts:445`) and add row renderers. **Confirm the exact kind strings and `details` keys against S9's spec before coding** — the names below are the plan's §A4.1/§A7 contract; S9 is the source of truth.

**SCOPE — the tree is a marker SKELETON, not a sub-agent transcript view (E5, plan §A11-E5):** sub-agents run in isolated context windows that aren't threaded into the parent's `messages` channel, so the server projection (S9) only surfaces these four **markers**. The tree therefore expands to **round / sub-agent / finding-and-failure markers** — it does **NOT** show, and v1 must **NOT promise**, a full sub-agent turn-by-turn conversation (tool calls, intermediate reasoning). Those full sub-agent traces live in **Langfuse**. Keep customer-facing copy and the scenario honest about this: the leaf rows are markers (`finding_id` + `source_url`, failure `reason`, started `prompt_preview`), not a nested conversation pane.

- `marker.supervisor.iteration` — `details: { iteration, subtasks_emitted, decision, reason }`
- `marker.subagent.started` — `details: { iteration, subtask, prompt_preview, tool_allowlist, depth }`
- `marker.subagent.finding` — `details: { iteration, subtask, finding_id, source_url }`
- `marker.subagent.failed` — `details: { iteration, subtask, reason }`

Render as an **expandable tree**, grouped client-side by `iteration` round → `subtask` → steps, using the existing `Fold` component:

- One **round group** per distinct `iteration`, wrapped in a `Fold` with `data-testid="activity-round-{i}-toggle"` (where `{i}` is the iteration number). Header shows the round number + the `supervisor.iteration` decision/`subtasks_emitted` summary.
- Inside each round, one **sub-agent group** per distinct `subtask`, wrapped in a nested `Fold` with `data-testid="activity-subagent-{subtask}"`. Header shows the subtask id + a status chip derived from whether a `finding` or `failed` marker arrived (so a round-2 retry of a round-1 failure reads as "subtask X: failed (round 1) → …"; design *Partial subagent failure*).
- Inside each sub-agent, the **steps** (its `started` / `finding` / `failed` markers) render as leaf rows. Findings show `finding_id` + `source_url`; failures show the `reason`. Use the per-row `DetailsAffordance` (`activity-row-<i>-expand` / `-details`, ActivityPane `:173`) for the raw `details` payload, consistent with existing markers.
- These markers are **infrastructure markers** — follow the existing `include_details` filtering convention (`ActivityPane` hides infra markers until the details toggle, per Scenario 19). Decide whether sub-agent markers are toggle-gated or always-visible by matching S9's projection intent; default to **always-visible** since they are the primary signal of a Deep Research run (not noise), and document the choice in the scenario.
- **No task-list change.** The tree lives *inside* one task's Activity pane; the task list is untouched (design: "5 sub-agents × 2 rounds is still 1 task, not 11").

## New / changed `data-testid`s (summary)

| `data-testid` | Surface | Element |
|---|---|---|
| `agent-config-preset` | Create (editable), Detail (read-only/disabled) | preset `<select>` |
| `agent-config-topology` | Create (read-only label), Detail (read-only) | derived topology display |
| `supervisor-config-max-fanout` | Create + Detail (within `SupervisorConfigSection`) | number input |
| `supervisor-config-max-iterations` | " | number input |
| `supervisor-config-source-allowlist` | " | chip input |
| `supervisor-config-writer-style` | " | `<select>` |
| `supervisor-config-scope-clarification` | " | checkbox |
| `activity-round-{i}-toggle` | Task Detail Activity pane | round-group `Fold` toggle |
| `activity-subagent-{subtask}` | Task Detail Activity pane | sub-agent-group `Fold` toggle |

## Affected Components

- **Service/Module:** Console — Agents + Task Detail
- **File paths:**
  - `services/console/src/features/agents/SupervisorConfigSection.tsx` (**new**)
  - `services/console/src/features/agents/CreateAgentDialog.tsx` (modify — preset `<select>`, conditional `SupervisorConfigSection`, payload assembly)
  - `services/console/src/features/agents/AgentDetailPage.tsx` (modify — preset/topology read-only on read + edit; mount `SupervisorConfigSection` read-only when `topology = supervisor`)
  - `services/console/src/features/task-detail/ActivityPane.tsx` (modify — `marker.subagent.*` / `marker.supervisor.iteration` tree renderers, grouped by round → subtask)
  - `services/console/src/types/index.ts` (modify — extend `AgentConfig` with `preset?` + `supervisor?: SupervisorConfig`; add `SupervisorConfig` interface; extend `ActivityEventKind` union with the four new markers)
  - `services/console/src/features/agents/__tests__/SupervisorConfigSection.test.tsx` (**new**)
  - `services/console/src/features/task-detail/__tests__/ActivityPane.test.tsx` (modify or new — sub-agent tree cases)
  - `docs/CONSOLE_BROWSER_TESTING.md` (modify — new scenario + coverage-matrix row + selection-matrix row)
- **Change type:** new section component + dialog/detail integration + activity-tree renderers + type extension + unit tests + new browser scenario

## Dependencies

- **Must complete first:**
  - **S1** (`task-s1-api-topology-preset-config.md`) — the `topology` / `preset` / `supervisor` field names, bounds, enum values, and immutability semantics the Console renders. Do not invent field names; take them from S1.
  - **S9** (`task-s9-observability-events.md`) — the exact `marker.subagent.*` / `marker.supervisor.iteration` kind strings and `details` keys the tree consumes.
- **Parallel-safe with:** Java/worker tasks (different code area). **Worktree-isolate** if any other Stream-P or Stream-S Console task touches `ActivityPane.tsx` or `types/index.ts` concurrently (plan §A3 / AGENTS.md §Parallel Subagent Safety — both are shared files).
- **Provides output to:** S11 (Supervisor integration + Playwright scenario, run by orchestrator).

## Implementation Specification

### `SupervisorConfigSection.tsx`

```tsx
export interface SupervisorConfig {
  max_fanout_per_iteration?: number;
  max_iterations?: number;
  source_allowlist?: string[];
  writer_style?: 'formal_report' | 'annotated_bullets';
  scope_clarification_enabled?: boolean;
}

interface Props {
  value: SupervisorConfig | undefined;
  onChange: (next: SupervisorConfig) => void;
  errors?: FieldErrors;
  disabled?: boolean;   // true on the Detail edit form (read-only-on-edit posture)
}
```

Mirror `ContextManagementSection.tsx` structure verbatim (the chip-input + Enter-handler + `data-chip` removal affordance for `source_allowlist`; the native `<select>` for `writer_style`; the checkbox for `scope_clarification_enabled`). Render nothing (`return null`) when the section is not applicable so the gate is purely the parent's `topology === 'supervisor'` check.

### `CreateAgentDialog.tsx` integration

- Add `preset` to the zod schema (default `'chat'`) and a `<select data-testid="agent-config-preset">`.
- `const preset = form.watch('preset')`; derive `topology = preset === 'research' ? 'supervisor' : 'react'` for the read-only `agent-config-topology` label and the section gate.
- Conditionally render `<SupervisorConfigSection>` when `topology === 'supervisor'`.
- Payload: include `preset` always; include `supervisor` sub-object only when the section is shown AND a field was set (mirror the `contextManagementPayload` only-if-dirty pattern at `:160-175`). Send `topology` only if S1's contract requires the client to send it (prefer preset-only).

### `AgentDetailPage.tsx` integration

- Read-only view: render preset + topology as plain text (`agent-config-preset` / `agent-config-topology`); render the supervisor sub-object read-only when present (mirror how the read-only view shows `context_management`).
- Edit mode: render `agent-config-preset` / `agent-config-topology` **`disabled`** (or as static text) — **no change handler**. Mount `<SupervisorConfigSection disabled />` (or read-only) when `topology === 'supervisor'`. A note: "Topology is fixed at agent creation."

### `ActivityPane.tsx` integration

- Extend the `event.kind` dispatch (the renderer switch around `:216-535`) with the four new marker kinds.
- Build the grouping client-side: a pre-pass over `events` partitions `marker.supervisor.iteration` + `marker.subagent.*` by `details.iteration` then `details.subtask`, preserving stream order; render nested `Fold`s (`activity-round-{i}-toggle` → `activity-subagent-{subtask}` → leaf step rows). Non-sub-agent events render exactly as today (no regression).
- Each leaf row keeps the existing `data-testid={`activity-row-${index}`}` + `data-kind` + `DetailsAffordance` so the raw `details` payload stays inspectable.

## Acceptance Criteria

**Functional:**
- [ ] `make console-test` passes, including the new `SupervisorConfigSection.test.tsx` and the `ActivityPane` sub-agent-tree cases.
- [ ] Create dialog renders `agent-config-preset` with the four presets; the `research` option label reads "Deep Research"; default selection is `chat`.
- [ ] Selecting `research` reveals `SupervisorConfigSection` with all five fields in order, each with its `data-testid`; selecting any other preset hides it.
- [ ] `agent-config-topology` renders the derived topology read-only on Create and is **disabled / non-editable** on the Detail page in both read-only and edit modes (invariant #2). No preset/topology change control exists on the Detail edit form.
- [ ] Create submit payload carries `agent_config.preset`; carries `agent_config.supervisor` only when the section is shown and a field was set (only-send-what-was-set verified via an API-client spy).
- [ ] `source_allowlist` chip input caps at 50 entries with inline "Maximum 50 entries" on the 51st; `writer_style` `<select>` offers exactly the two enum values; `max_fanout_per_iteration` / `max_iterations` are number inputs.
- [ ] `ActivityPane` renders `marker.subagent.*` / `marker.supervisor.iteration` as an expandable tree grouped round → sub-agent → steps, with `activity-round-{i}-toggle` and `activity-subagent-{subtask}` toggles; a round-2 retry of a failed round-1 `subtask` links visibly to its earlier attempt (status chip), not as two unrelated entries. **The leaves are marker rows (skeleton — E5), not a sub-agent conversation view**: the tree expands to round/sub-agent/finding/failure markers only; no full sub-agent transcript is rendered (those are in Langfuse). The scenario/copy does not promise one.
- [ ] Non-sub-agent Activity rendering is unchanged (no regression in existing turn/marker rows).
- [ ] No `console` errors on initial render or field changes (the orchestrator confirms via `browser_console_messages`).

**Console merge-gate checklist (copied verbatim from [`../../../CONSOLE_TASK_CHECKLIST.md`](../../../../../CONSOLE_TASK_CHECKLIST.md) — obey all):**
- [ ] Named the scenario template(s) — this task uses **B** (new dialog control + sub-agent tree = new scenario) **and D** (preset/topology + the `supervisor` sub-object render on **both** Create and Agent Detail → cross-cutting parity required).
- [ ] Listed the §Agent-Config Coverage Matrix cells the change touches and the scenario numbers they cite after merge (see below). No `⚠ gap` left open.
- [ ] Every new interactive element has a stable `data-testid` (see the table above).
- [ ] Added a new scenario asserting fields **at the field + `data-testid` level** (not "the section renders"). The scenario diff is in the merge commit.
- [ ] Same commit updates the §When to Run Which Scenarios selection matrix.
- [ ] Template D's four parity assertions appear in the scenario because preset/topology + `supervisor` render on >1 surface (Create + Detail).
- [ ] The merge commit / PR names the scenario numbers run in the browser, and the **orchestrator (not this subagent)** runs them. **This subagent does NOT tick this box.**

**Docs (in the same commit):**
- [ ] New scenario added to `CONSOLE_BROWSER_TESTING.md` (next free index, currently 20+) covering: the research-preset create flow (preset select → `SupervisorConfigSection` reveal → all five fields persist round-trip), topology read-only-on-edit on the Detail page (Template D parity), and the sub-agent activity tree (round → subtask → steps expand; failed→retry linkage).
- [ ] §Agent-Config Coverage Matrix gains a new row **`supervisor` / `agent_mode`**: `Create Dialog = <new#>`, `Agent Detail = <new#>` (read-only parity), `Edit Form = <new#>` (read-only), `Submit (read-only) = —`, `Task Detail = <new#>` (the sub-agent tree). Populate every cell in the same commit.
- [ ] §When to Run Which Scenarios gains a row: `Agent supervisor / Deep-Research feature → 1, 2, <new#>` (smoke + agent CRUD + the new scenario).

## Testing Requirements

- **Unit (`SupervisorConfigSection.test.tsx`):** each field renders with its initial value + `data-testid`; `onChange` fires with the updated `SupervisorConfig` shape; chip-input cap at 50 with inline error; `writer_style` offers exactly the two enum values; `disabled` prop renders all controls non-editable; only-send-what-was-set (an untouched section produces no `supervisor` key in the submit payload, verified via API-client spy).
- **Unit (`CreateAgentDialog`):** selecting `research` reveals the section and emits `preset: "research"` in the payload; selecting `chat` hides it and omits `supervisor`.
- **Unit (`ActivityPane`):** a fixture event list with two iterations and a failed-then-retried subtask renders the expected nested `Fold`s with the round/subtask `data-testid`s and the correct status chips; an unknown marker kind is tolerated (forward-compat, matching the existing renderer).
- **No live Playwright run in this task.** Produce the scenario manifest text; the orchestrator executes Playwright per AGENTS.md §Browser Verification.

## Constraints and Guardrails

- **Topology is read-only on edit** — never render an editable preset/topology control on the Detail page. This is invariant #2; an editable control is a plan failure (plan §A0 #2, design *Two-layer naming*).
- **No provider/topology branching beyond the show/hide gate and the pre-shaped data render.** The Console renders the server's pre-normalized `supervisor` sub-object and pre-projected activity events; it must not parse provider block shapes or contain `if (topology === ...)` logic beyond gating the section's visibility and grouping the tree (AGENTS.md §LLM Provider Support: the Console renders pre-normalized strings only).
- **Customer-facing copy uses "Deep Research", never "Supervisor".** The internal term `supervisor` appears only in `data-testid`s / payload keys / code, never in visible labels (design *Two-layer naming*).
- **Do not add a new API client method** for the activity tree — the markers ride the existing `listActivity` (`client.ts:249`) `ActivityListResponse`.
- **Do not** call Playwright MCP tools or `make start` / `make stop`.
- **Worktree-isolate** if a parallel Console task touches `ActivityPane.tsx` / `types/index.ts` (shared files, plan §A3).

## Assumptions

- S1 has shipped the `topology` / `preset` / `supervisor` config contract (field names + bounds + enum values + immutability) and `AgentConfig` on the API returns them; this task takes the names from S1 verbatim. If S1 sends `topology` explicitly rather than deriving it from `preset`, follow S1.
- S9 has shipped the `marker.subagent.*` / `marker.supervisor.iteration` projection into `ActivityListResponse` with `iteration` + `subtask` in `details`; this task takes the kind strings + `details` keys from S9 verbatim.
- The Console test harness is React Testing Library + Vitest (the harness `ContextManagementSection.test.tsx` already uses).
- The `research` preset's defaults (fan-out width 5, low concurrency) are seeded server-side by S2; the Console only renders/overrides what the customer changes.

<!-- AGENT_TASK_END: task-s10-console-supervisor.md -->
