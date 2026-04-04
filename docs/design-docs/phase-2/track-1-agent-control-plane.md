# Track 1 Design — Agent Control Plane

## Context

Phase 1 treats agent configuration as inline task submission data. Clients submit `agent_id` plus a full `agent_config`, the API validates the payload, and the task row stores the config directly in `agent_config_snapshot`. This keeps execution simple, but it means agent identity is not yet a real control-plane resource.

Track 1 establishes Agent as a first-class entity. The goal is not to deliver the full Phase 2 runtime all at once; it is to create the clean control-plane boundary that later scheduling, budget, memory, and approval features will build on.

This track is intentionally end-to-end:

1. Agents become a stored resource in the backend
2. Task submission switches from inline config to selecting an existing agent
3. The Console gets a dedicated Agents area
4. Existing task execution safety is preserved through snapshot-at-submission semantics

## Goals

- Make Agent the canonical source of truth for reusable runtime configuration
- Replace inline `agent_config` task submission with agent-based submission
- Provide a user-facing control plane for agent CRUD and lifecycle management
- Preserve existing task execution stability by keeping per-task snapshots
- Keep the track lean enough to stand on its own before scheduler and memory work begins

## Non-Goals

Track 1 does not include:

- agent budgets
- agent concurrency limits
- long-term memory references or compaction
- pause semantics
- approval workflows
- BYOT / custom tool runtime behavior
- hard deletion of agents
- data-preserving migration requirements for existing development environments

## Core Decisions

- `agent_id` remains the unique stable identifier used by APIs, task submission, filtering, and routing.
- `display_name` is added as a user-facing label and is not required to be unique.
- Agent lifecycle in Track 1 supports only `active` and `disabled`.
- Disabling an agent blocks new task submissions only. Existing tasks are unaffected.
- Agent edits affect future tasks only.
- Tasks continue to snapshot the resolved agent configuration at submission time.
- Tasks also snapshot the agent display name so historical task views remain stable if the agent is renamed later.
- Task submission remains responsible for task-scoped runtime fields such as input, retries, max steps, timeout, and Langfuse endpoint selection.
- The public task submission contract becomes agent-based only. Inline `agent_config` is removed.
- The Console gets both an Agents list route and an Agent detail route.

## Data Model

### New table: `agents`

Track 1 adds an `agents` table as the source of truth for reusable agent configuration.
It intentionally introduces only the control-plane subset of the broader Phase 2 Agent entity. Later tracks extend this resource with fields such as memory references, concurrency controls, and budgets once those behaviors are implemented.

| Column | Type | Constraints / Meaning |
|--------|------|------------------------|
| `tenant_id` | `TEXT` | NOT NULL, reserved for future auth scoping |
| `agent_id` | `TEXT` | NOT NULL |
| `display_name` | `TEXT` | NOT NULL, max 200 characters, human-facing label, not required to be unique |
| `agent_config` | `JSONB` | NOT NULL, stores the same config shape currently used by Phase 1 task submission and `agent_config_snapshot`: `system_prompt`, `provider`, `model`, `temperature`, and `allowed_tools` |
| `status` | `TEXT` | NOT NULL, `active` or `disabled`, defaults to `active` |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, defaults to now |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, defaults to now |

Primary key: `(tenant_id, agent_id)` — composite key matching the task table's multi-tenant shape.

`tenant_id` remains part of the schema even though Phase 1 still resolves it internally to `"default"`. This keeps the Agent resource aligned with the task schema’s forward-compatible multi-tenant shape.

### Task table changes

The `tasks` table remains the durable execution record for submitted work. Track 1 keeps the existing `agent_config_snapshot` field and adds one more snapshot field:

| Column | Meaning |
|--------|---------|
| `agent_id` | Stable agent identifier chosen at submission |
| `agent_config_snapshot` | Resolved config copied from the Agent record at submission time |
| `agent_display_name_snapshot` | Display label copied from the Agent record at submission time |

This preserves the Phase 1 execution model:

- **Foreign Key Constraint:** A composite foreign key `FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, agent_id)` should be added to the `tasks` table to enforce referential integrity.
- task execution is based on immutable task-owned data
- agent records are mutable only for future submissions
- historical task views remain stable even after agent edits
- the presence of task snapshots does not imply that list endpoints should return the full snapshot payload by default

### Status model

Track 1 Agent status values:

- `active`: selectable for new task submission
- `disabled`: visible and editable, but not selectable for new task submission

Track 1 does not introduce `paused`. That status belongs to later scheduler/budget behavior, and should not appear before it has real runtime meaning.

## API Design

### New Agent resource

Track 1 adds a new REST resource at `/v1/agents`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/agents` | Create agent |
| `GET` | `/v1/agents` | List agents |
| `GET` | `/v1/agents/{agent_id}` | Get full agent detail |
| `PUT` | `/v1/agents/{agent_id}` | Update config or status |

Track 1 does not add delete endpoints.

### Create agent

Request body:

```json
{
  "agent_id": "support-agent-v1",
  "display_name": "Support Agent",
  "agent_config": {
    "system_prompt": "You are a helpful support assistant.",
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest",
    "temperature": 0.7,
    "allowed_tools": ["web_search", "read_url"]
  }
}
```

Behavior:

- new agents are created with `status = "active"`
- `agent_id` must be unique within the tenant
- `agent_id` uses the same maximum length currently applied to task submission and should be restricted to a path-safe slug format because it is used in both API and Console route parameters
- `agent_config` uses the same validation rules already enforced for Phase 1 task submission

Response body:

```json
{
  "agent_id": "support-agent-v1",
  "display_name": "Support Agent",
  "agent_config": {
    "system_prompt": "You are a helpful support assistant.",
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest",
    "temperature": 0.7,
    "allowed_tools": ["web_search", "read_url"]
  },
  "status": "active",
  "created_at": "2026-03-31T10:00:00Z",
  "updated_at": "2026-03-31T10:00:00Z"
}
```

### List agents

Query parameters:

- `status` (optional): filter by agent status (`active`, `disabled`)
- `limit` (optional, default 50, max 200): maximum number of agents to return

List responses should stay lightweight and summary-oriented. They should include the fields needed for selection, overview, and navigation, but not the full prompt/config payload.

```json
[
  {
    "agent_id": "support-agent-v1",
    "display_name": "Support Agent",
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest",
    "status": "active",
    "created_at": "2026-03-31T10:00:00Z",
    "updated_at": "2026-03-31T10:00:00Z"
  }
]
```

### Get agent detail

Returns the full editable agent record, including `agent_config` with prompt and allowed tools.

This detail response is the source for:

- the Agent detail page
- any submit-page read-only preview that needs more than the lightweight list summary

### Update agent

Request body:

```json
{
  "display_name": "Support Agent",
  "agent_config": {
    "system_prompt": "You are a helpful support assistant.",
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest",
    "temperature": 0.5,
    "allowed_tools": ["web_search", "read_url", "calculator"]
  },
  "status": "disabled"
}
```

Behavior:

- `agent_id` is immutable
- PUT uses full-replacement semantics: the client must send all mutable fields; omitted fields are not preserved
- updates may change `display_name`, `agent_config`, and `status`
- setting `status = "disabled"` blocks future submissions but does not modify existing tasks

### Task submission contract change

`POST /v1/tasks` changes from inline-agent submission to stored-agent submission.

New request body:

```json
{
  "agent_id": "support-agent-v1",
  "input": "Draft a response to ticket 123.",
  "max_retries": 3,
  "max_steps": 25,
  "task_timeout_seconds": 3600,
  "langfuse_endpoint_id": "optional-endpoint-id"
}
```

Removed from the public contract:

- `agent_config`

Behavior:

- the API resolves the agent by `agent_id`
- the agent must exist and be `active`
- the API snapshots the resolved config into `tasks.agent_config_snapshot`
- the API snapshots `display_name` into `tasks.agent_display_name_snapshot`
- task-level runtime fields remain task-owned and are not moved onto the Agent resource

Submission response should include both identity fields:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent_id": "support-agent-v1",
  "agent_display_name": "Support Agent",
  "status": "queued",
  "created_at": "2026-03-31T10:05:00Z"
}
```

### Task response enrichment

Any task-facing response that currently exposes `agent_id` should also expose `agent_display_name`.

This includes the response families used by:

- task submission
- task list
- task detail / status
- dead-letter list
- observability views where agent identity is shown

The value should come from the task snapshot, not from a live join to the `agents` table.

Task list-style responses should remain lightweight. Track 1 does not require task list endpoints to return the full `agent_config_snapshot`. The snapshot exists for execution stability and historical accuracy, not because every list or overview view needs the full submitted configuration payload.

If richer visibility into configuration is needed in the UI:

- live agent configuration should come from the Agent detail route
- task-specific submitted configuration can be added to task detail later if needed, without changing the lightweight list contract

## Lifecycle and Runtime Semantics

### Agent edits

Agent edits apply only to future submissions. Once a task is created:

- its `agent_id` remains fixed
- its `agent_config_snapshot` remains fixed
- its `agent_display_name_snapshot` remains fixed

This matches the existing Phase 1 safety model and avoids ambiguous behavior when long-running tasks outlive agent edits.

### Disabled agents

Disabled agents:

- remain visible in list and detail views
- remain editable
- cannot be used for new task submission

Disabled agents do not:

- cancel queued tasks
- stop running tasks
- rename or rewrite historical tasks

### No hard delete

Track 1 intentionally excludes hard delete. The control plane should preserve agent identity for auditability and future task history views, and later phases can decide whether limited deletion rules are worth adding.

## Console UX

### Navigation

Track 1 adds a dedicated Agents area to the Console:

- `/agents`
- `/agents/:agentId`

This is a first-class navigation destination, not a Settings subsection.

### Agents list page

The Agents list page is the management entrypoint. It should provide:

- a list/table of agents
- display name as the primary label
- `agent_id` as visible secondary identity
- configuration summary (provider, model) extracted from `agent_config`
- current status
- create action
- navigation into the Agent detail route when an agent row or primary label is selected

Create should happen from the list page. A dialog is acceptable for creation because creation is short and the list page is the control-plane entrypoint.

### Agent detail page

The Agent detail route exists because agents are expected to grow beyond a tiny CRUD object over time.

Track 1 detail page scope:

- show the full current agent configuration
- allow editing of config fields
- allow changing status between `active` and `disabled`
- show both `display_name` and unique `agent_id`
- include a “Submit task” CTA (hidden or visually disabled when the agent's status is `disabled`)

The “Submit task” CTA should navigate to `/tasks/new?agent_id=<id>` so the submit flow opens with that agent preselected.

Track 1 does not require task-history panels on the agent detail page. Task history remains on the existing task pages.

### Submit page

The submit page changes from “define an agent inline” to “run a task with an existing agent.”

It should:

- let the user select an existing active agent
- show both display name and `agent_id` in the selector
- after selection, fetch agent detail as needed and show a read-only summary of the agent's config (provider, model, tools, and system prompt) so the user knows what they are submitting with
- preserve task-level controls such as retries, max steps, timeout, and Langfuse endpoint
- preserve the task input field

If an `agent_id` query parameter is present:

- preselect the matching active agent
- if the referenced agent is missing or disabled, show an error state and require the user to choose another active agent

If no agents exist:

- the submit page should remain accessible and show an inline empty state
- the empty state should explain that no agents currently exist and that an agent must be created before a task can be submitted
- the empty state may link to the Agents area, but it does not redirect automatically
- the submit page does not offer inline agent creation or inline agent configuration

### Task presentation

Task list/detail/failed-task views should use this display convention:

- `display_name` as the primary visible label
- `agent_id` as secondary identifying text

This gives the UI a friendlier default while preserving the unique identifier users need for debugging and filtering.

Where task views present agent identity, the Console should support navigating to the corresponding Agent detail route so users can inspect the current live agent definition without requiring list endpoints to embed full agent configuration.

## Validation and Consistency Rules

- inside `agent_config`, `provider` and `model` must remain valid against the active models registry
- `allowed_tools` must remain valid against the current tool whitelist
- agent updates use the same config validation model as agent creation
- task submission rejects:
  - unknown `agent_id`
  - disabled agents
  - task-level runtime values that violate existing task validation rules

Track 1 should reuse the existing config validation behavior wherever possible rather than creating a second incompatible validation model.

## Development Environment Assumption

Track 1 does not need to preserve existing development data. Schema changes may be folded into the existing SQL files rather than introduced as a new forward migration, as long as the repo remains internally consistent after the change.

## Acceptance Criteria

Track 1 is complete from a design perspective when the following are true:

1. A user can create and manage reusable agents without submitting a task
2. A new task cannot be submitted without choosing an existing active agent
3. Submitting a task snapshots both config and display name onto the task
4. Editing an agent changes future submissions only
5. Disabling an agent blocks new submissions but leaves existing tasks alone
6. The Console exposes both an Agents list page and an Agent detail page
7. Task views show both the human-friendly display name and the stable `agent_id`
