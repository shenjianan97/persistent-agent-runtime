<!-- AGENT_TASK_START: task-9-follow-up.md -->

# Task 9 — Task Follow-Up: Continue Completed Tasks

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Task Follow-Up section)
2. `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` — existing `respondToTask()` and `resumeTask()` patterns
3. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — existing HITL mutation methods
4. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` — existing task endpoints
5. `services/worker-service/executor/graph.py` — `run_astream()` resume path (lines ~550-570)
6. `services/console/src/features/task-detail/TaskDetailPage.tsx` — current task detail page
7. `services/console/src/features/task-detail/InputResponsePanel.tsx` — existing HITL input panel pattern
8. `services/console/src/api/client.ts` — existing API client methods

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Completed tasks retain their full LangGraph checkpoint history. This task adds the ability to continue a completed task by providing a follow-up prompt. The worker resumes from the last checkpoint with the full prior conversation context, injecting the follow-up as a new `HumanMessage`.

No new database columns are needed. The existing `human_response` TEXT column is reused with a new payload kind: `{"kind": "follow_up", "message": "..."}`.

## Task-Specific Shared Contract

- Reuse `human_response` column with `{"kind": "follow_up", "message": "..."}` payload
- Only tasks in `completed` status can be followed up
- The task ID stays the same — checkpoint history is preserved
- `output` is cleared on follow-up (new output will be written on completion)
- New event type: `task_follow_up` (completed → queued)
- Worker injects `{"messages": [HumanMessage(content=message)]}` as `initial_input` for LangGraph

## Affected Component

- **Service/Module:** API Service, Worker Service, Console
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (modify — add follow-up endpoint)
  - `services/api-service/src/main/java/com/persistentagent/api/service/TaskService.java` (modify — add `followUpTask()` method)
  - `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` (modify — add `followUpTask()` mutation)
  - `services/worker-service/executor/graph.py` (modify — add follow-up path in `run_astream()`)
  - `services/console/src/api/client.ts` (modify — add `followUpTask()` API method)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify — add follow-up UI panel)
  - `services/console/src/types/index.ts` (modify — add `task_follow_up` to TaskEventType if needed)
- **Change type:** modification

## Dependencies

- **Must complete first:** Tasks 1-8 (core Track 4 implementation)
- **Provides output to:** None (standalone feature)
- **Shared interfaces/contracts:** `human_response` payload format, task state machine

## Implementation Specification

### Step 1: Add follow-up mutation to TaskRepository

Add to `TaskRepository.java`, following the `respondToTask()` pattern:

```java
public HitlMutationResult followUpTask(UUID taskId, String tenantId, String humanResponse) {
    List<Map<String, Object>> rows = jdbcTemplate.queryForList(
        """
        UPDATE tasks SET
            status = 'queued',
            human_response = ?,
            output = NULL,
            lease_owner = NULL,
            lease_expiry = NULL,
            version = version + 1,
            updated_at = NOW()
        WHERE task_id = ?::uuid AND tenant_id = ? AND status = 'completed'
        RETURNING task_id, agent_id, worker_pool_id
        """,
        humanResponse, taskId.toString(), tenantId
    );
    if (rows.isEmpty()) {
        // Check if task exists but wrong state
        boolean exists = jdbcTemplate.queryForList(
            "SELECT 1 FROM tasks WHERE task_id = ?::uuid AND tenant_id = ?",
            taskId.toString(), tenantId
        ).size() > 0;
        return new HitlMutationResult(
            exists ? HitlMutationOutcome.WRONG_STATE : HitlMutationOutcome.NOT_FOUND,
            null, null
        );
    }
    Map<String, Object> row = rows.get(0);
    return new HitlMutationResult(
        HitlMutationOutcome.UPDATED,
        row.get("agent_id").toString(),
        (String) row.get("worker_pool_id")
    );
}
```

### Step 2: Add followUpTask to TaskService

Add to `TaskService.java`, following the `respondToTask()` pattern:

```java
@Transactional
public RedriveResponse followUpTask(UUID taskId, String input) {
    String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

    String humanResponse;
    String detailsJson;
    try {
        humanResponse = objectMapper.writeValueAsString(Map.of("kind", "follow_up", "message", input));
        detailsJson = objectMapper.writeValueAsString(Map.of("input", input));
    } catch (Exception e) {
        throw new RuntimeException("Failed to serialize follow-up payload", e);
    }

    TaskRepository.HitlMutationResult result = taskRepository.followUpTask(taskId, tenantId, humanResponse);
    return switch (result.result()) {
        case UPDATED -> {
            taskRepository.notifyNewTask(result.workerPoolId());
            taskEventService.recordEvent(tenantId, taskId, result.agentId(),
                    "task_follow_up", "completed", "queued",
                    null, null, null, detailsJson);
            yield new RedriveResponse(taskId, "queued");
        }
        case NOT_FOUND -> throw new TaskNotFoundException(taskId);
        case WRONG_STATE -> throw new InvalidStateTransitionException(taskId,
                "Task " + taskId + " cannot be followed up (must be in completed state)");
    };
}
```

### Step 3: Add follow-up endpoint to TaskController

Add to `TaskController.java`:

```java
@PostMapping("/{taskId}/follow-up")
public ResponseEntity<RedriveResponse> followUpTask(
        @PathVariable UUID taskId,
        @Valid @RequestBody TaskRespondRequest request) {
    RedriveResponse response = taskService.followUpTask(taskId, request.message());
    return ResponseEntity.ok(response);
}
```

This reuses the existing `TaskRespondRequest` record which has a `message` field.

### Step 4: Add follow-up path in worker execute_task()

In `services/worker-service/executor/graph.py`, modify the resume path in `run_astream()`:

```python
# Resume path: if this is a resumed task with a human response, use Command(resume=...)
if not is_first_run:
    human_response = await self.pool.fetchval(
        'SELECT human_response FROM tasks WHERE task_id = $1::uuid', task_id
    )
    if human_response:
        payload = json.loads(human_response)
        if payload.get("kind") == "follow_up":
            # Follow-up: inject new HumanMessage into existing conversation
            initial_input = {"messages": [HumanMessage(content=payload.get("message", ""))]}
        elif payload.get("kind") == "input":
            resume_value = payload.get("message", "")
            initial_input = Command(resume=resume_value)
        else:
            resume_value = payload
            initial_input = Command(resume=resume_value)
```

### Step 5: Add follow-up API client method

Add to `services/console/src/api/client.ts`:

```typescript
followUpTask: (taskId: string, input: string) =>
    fetchApi<RedriveResponse>(`/v1/tasks/${encodeURIComponent(taskId)}/follow-up`, {
        method: 'POST',
        body: JSON.stringify({ message: input }),
    }),
```

### Step 6: Add follow-up UI panel to TaskDetailPage

Add a follow-up panel to `TaskDetailPage.tsx` when `status === 'completed'`:

- Text area for follow-up input
- "Continue" button that calls `followUpTask()`
- On success: invalidate queries, show toast
- Styling: match the existing `InputResponsePanel` pattern

Add after the output card section:

```typescript
{task.status === 'completed' && (
    <Card className="console-surface border-primary/30 relative overflow-hidden">
        <div className="absolute top-0 left-0 w-1 h-full bg-primary" />
        <CardHeader className="border-b border-primary/20 pb-3">
            <CardTitle className="text-sm font-display uppercase tracking-widest text-primary flex items-center gap-2">
                <MessageSquare className="w-4 h-4" /> Follow Up
            </CardTitle>
        </CardHeader>
        <CardContent className="pt-4 space-y-4">
            <p className="text-xs text-muted-foreground">
                Continue this task with a follow-up prompt. The agent will resume from its last state with full conversation context.
            </p>
            <Textarea
                className="min-h-[80px] resize-y rounded-none border-border bg-black/50 focus-visible:ring-primary"
                placeholder="Ask a follow-up question or provide additional instructions..."
                value={followUpInput}
                onChange={(e) => setFollowUpInput(e.target.value)}
            />
            <div className="flex justify-end">
                <Button
                    onClick={handleFollowUp}
                    disabled={followUpMutation.isPending || !followUpInput.trim()}
                    className="font-bold uppercase tracking-widest px-6"
                >
                    {followUpMutation.isPending ? 'Submitting...' : 'Continue'}
                </Button>
            </div>
        </CardContent>
    </Card>
)}
```

Add the state and mutation:

```typescript
const [followUpInput, setFollowUpInput] = useState('');
const followUpMutation = useMutation({
    mutationFn: ({ taskId, input }: { taskId: string; input: string }) =>
        api.followUpTask(taskId, input),
    onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: ['task', taskId] });
        queryClient.invalidateQueries({ queryKey: ['task-events', taskId] });
        queryClient.invalidateQueries({ queryKey: ['checkpoints', taskId] });
        setFollowUpInput('');
        toast.success('Follow-up submitted', { description: 'Task will resume with your input.' });
    },
    onError: (error: Error) => {
        toast.error('Follow-up failed', { description: error.message });
    },
});

const handleFollowUp = () => {
    if (!taskId || !followUpInput.trim()) return;
    followUpMutation.mutate({ taskId, input: followUpInput.trim() });
};
```

### Step 7: Add task_follow_up to event types

If `TaskEventType` in `services/console/src/types/index.ts` is a union type, add `'task_follow_up'`.

Add to `CheckpointTimeline.tsx` HITL event types and styles:

```typescript
// In HITL_EVENT_TYPES set:
'task_follow_up',

// In HITL_STYLES:
task_follow_up: { label: 'Follow Up', colorClass: 'text-primary', bgClass: 'bg-primary', icon: MessageSquare },
```

### Step 8: Write tests

**API tests:**
- `testFollowUpTask_completed_success` — follow-up on completed task returns 200 with `queued` status
- `testFollowUpTask_running_fails` — follow-up on running task returns error (wrong state)
- `testFollowUpTask_notFound` — follow-up on non-existent task returns 404

**Worker tests:**
- `test_follow_up_injects_human_message` — verify that `kind: follow_up` injects `HumanMessage` not `Command(resume=...)`

**Console tests:**
- Existing tests should still pass (follow-up UI only shown for completed tasks)

## Acceptance Criteria

- [ ] `POST /v1/tasks/{task_id}/follow-up` transitions `completed` → `queued` with follow-up payload
- [ ] Follow-up on non-completed task returns appropriate error
- [ ] Worker detects `kind: follow_up` and injects `HumanMessage` into existing conversation
- [ ] Task resumes from last checkpoint with full prior context
- [ ] New checkpoints are appended to existing timeline
- [ ] `task_follow_up` event recorded in task events
- [ ] Console shows "Follow Up" panel on completed tasks
- [ ] Follow-up input clears and task transitions to running view on submit
- [ ] `task_follow_up` appears in the execution timeline
- [ ] `output` is cleared on follow-up (new output written on re-completion)
- [ ] All existing tests pass (no regressions)

## Testing Requirements

- **Unit tests:** API endpoint validation (completed only, not found, wrong state). Worker follow-up path detection.
- **Integration tests:** Submit task, let it complete, follow up, verify new checkpoints appended and new output generated.
- **Console:** Follow-up panel appears on completed tasks, submit works, timeline shows follow-up event.

## Constraints and Guardrails

- Do not add new database columns — reuse `human_response` with `{"kind": "follow_up", ...}`
- Do not modify checkpoint data — LangGraph handles appending naturally
- Do not change existing HITL resume behavior — follow-up is a new `kind`, not a replacement
- Reuse `TaskRespondRequest` for the API request body (it already has a `message` field)
- The follow-up clears `output` but preserves `input` (original task input) and all checkpoints

## Assumptions

- Tasks 1-8 have been completed (Track 4 core implementation)
- LangGraph supports calling `astream({"messages": [HumanMessage(...)]}, config)` on an existing thread to continue conversation
- The `human_response` column is cleared by the worker after processing (existing behavior)
- The `HitlMutationResult` record and `HitlMutationOutcome` enum from TaskRepository are reusable

<!-- AGENT_TASK_END: task-9-follow-up.md -->
