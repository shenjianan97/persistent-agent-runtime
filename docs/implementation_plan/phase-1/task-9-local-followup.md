# Task 9 Local Follow-Up

This note tracks the **local-first** Langfuse cleanup work that was completed after the original Task 9 spec was written, without changing the original Task 9 document.

## Completed Local Follow-Up
- Local Langfuse support is wired into the worker, API, and console.
- `GET /v1/tasks/{taskId}/observability` exists and is now the customer-facing task-detail execution contract.
- Task/list `total_cost_microdollars` values are sourced from observability totals.
- The task detail page now renders a single `Execution` view instead of separate observability and checkpoint timeline panels.
- The unified `Execution` response includes:
  - Langfuse spans
  - durable runtime markers such as checkpoint persisted, resumed after retry, and dead-letter/completion markers
- Historical terminal tasks with no Langfuse trace now render an explicit no-trace state instead of an “awaiting” state.
- Local `make start` now treats Langfuse as part of the default stack.
- Local startup now fails fast when Langfuse is unreachable, and the task detail UI separates `Key steps` from `Durable progress` so checkpoints no longer compete with model/tool calls.

## Deferred Follow-Up
- AWS/CDK Langfuse deployment
- CloudWatch operator metrics, dashboards, and alarms
- Destructive checkpoint schema cleanup (`cost_microdollars`, `execution_metadata`)

## Notes
- The original Task 9 document in `agent_tasks/` remains the broader source of intent.
- This file exists only to track the local implementation direction and the follow-up decisions made during local verification.
