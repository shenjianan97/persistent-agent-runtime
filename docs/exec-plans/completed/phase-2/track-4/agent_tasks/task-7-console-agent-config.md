<!-- AGENT_TASK_START: task-7-console-agent-config.md -->

# Task 7 — Console: Agent Config Editor — Tool Server Multi-Select

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Console Design → Agent config editor section)
2. `services/console/src/features/agents/CreateAgentDialog.tsx` — current create dialog with tools checkboxes
3. `services/console/src/features/agents/AgentDetailPage.tsx` — current detail page with edit form
4. `services/console/src/features/tool-servers/useToolServers.ts` — Task 6 output: `useToolServers()` hook
5. `services/console/src/types/index.ts` — current type definitions (verify `AgentConfig` includes `tool_servers`)

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Track 4 adds a "Tool Servers" section to the agent create and edit forms. This section displays a multi-select control listing all active tool servers for the tenant, allowing operators to assign external tool servers to agents alongside built-in tools.

The section sits between the existing Tools checkboxes and the Human-in-the-Loop toggle in both the `CreateAgentDialog` and the `AgentDetailPage` edit form.

## Task-Specific Shared Contract

- The `tool_servers` field in agent config is an array of server name strings.
- The multi-select fetches active servers via `GET /v1/tool-servers?status=active`.
- Each option shows: `"{name} — {url}"` for clarity.
- Empty selection is valid (agent uses only built-in tools).
- If no tool servers are registered, show hint text: "No tool servers registered. Register one in Tool Servers to give this agent custom tools."
- Both `CreateAgentDialog` and `AgentDetailPage` edit form must be updated.
- The read-only view on `AgentDetailPage` should display the assigned tool servers (name list).

## Affected Component

- **Service/Module:** Console — Agent Feature
- **File paths:**
  - `services/console/src/features/agents/CreateAgentDialog.tsx` (modify — add tool servers section)
  - `services/console/src/features/agents/AgentDetailPage.tsx` (modify — add tool servers to read-only and edit views)
- **Change type:** modification

## Dependencies

- **Must complete first:** Task 2 (Tool Server API — list endpoint), Task 3 (Agent Config Extension — `tool_servers` in type definitions)
- **Provides output to:** Task 8 (Integration Tests — console tests)
- **Shared interfaces/contracts:** `useToolServers()` hook from Task 6, `AgentConfig.tool_servers` from Task 3

## Implementation Specification

### Step 1: Update CreateAgentDialog form schema

In `services/console/src/features/agents/CreateAgentDialog.tsx`, add `tool_servers` to the Zod schema:

```typescript
const createAgentSchema = z.object({
    // ... existing fields ...
    display_name: z.string().min(1, 'Agent name is required').max(200),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).default(0.7),
    allowed_tools: z.array(z.string()).default([]),
    tool_servers: z.array(z.string()).default([]),   // <-- ADD THIS
    max_concurrent_tasks: z.number().int().min(1).default(5),
    budget_max_per_task: z.number().int().min(1).default(500000),
    budget_max_per_hour: z.number().int().min(1).default(5000000),
});
```

Update the default values to include `tool_servers: []`.

### Step 2: Add tool servers multi-select to CreateAgentDialog

Add the Tool Servers section between the Tools checkboxes and the Human-in-the-Loop toggle. Import `useToolServers` from the tool-servers feature:

```typescript
import { useToolServers } from '../tool-servers/useToolServers';

// Inside the component:
const { data: toolServers = [] } = useToolServers('active');
const selectedToolServers = form.watch('tool_servers');

// In the form JSX, after the Tools checkboxes section:
<div className="space-y-3">
    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">
        Tool Servers
    </FormLabel>
    {toolServers.length === 0 ? (
        <p className="text-muted-foreground text-xs">
            No tool servers registered. Register one in Tool Servers to give this agent custom tools.
        </p>
    ) : (
        <div className="space-y-2">
            {toolServers.map((server) => (
                <label
                    key={server.server_id}
                    className="flex items-center gap-3 p-2 rounded hover:bg-white/5 cursor-pointer"
                >
                    <input
                        type="checkbox"
                        checked={selectedToolServers.includes(server.name)}
                        onChange={(e) => {
                            const current = form.getValues('tool_servers');
                            if (e.target.checked) {
                                form.setValue('tool_servers', [...current, server.name]);
                            } else {
                                form.setValue('tool_servers', current.filter((n) => n !== server.name));
                            }
                        }}
                        className="accent-primary"
                    />
                    <div>
                        <span className="text-sm font-medium">{server.name}</span>
                        <span className="text-muted-foreground text-xs ml-2">— {server.url}</span>
                    </div>
                </label>
            ))}
        </div>
    )}
</div>
```

### Step 3: Include tool_servers in the submit payload

Update the `onSubmit` function to include `tool_servers` in the request body:

```typescript
function onSubmit(data: CreateAgentFormValues) {
    mutation.mutate(
        {
            display_name: data.display_name,
            agent_config: {
                system_prompt: data.system_prompt,
                provider: data.provider,
                model: data.model,
                temperature: data.temperature,
                allowed_tools: data.allowed_tools,
                tool_servers: data.tool_servers,    // <-- ADD THIS
            },
            max_concurrent_tasks: data.max_concurrent_tasks,
            budget_max_per_task: data.budget_max_per_task,
            budget_max_per_hour: data.budget_max_per_hour,
        },
        { /* existing onSuccess/onError callbacks */ }
    );
}
```

### Step 4: Update AgentDetailPage read-only view

In the read-only section of `AgentDetailPage.tsx`, add a Tool Servers display after the tools display:

```typescript
{/* Tool Servers (read-only) */}
{agent?.agent_config?.tool_servers && agent.agent_config.tool_servers.length > 0 && (
    <div className="space-y-2">
        <div className="uppercase tracking-widest text-muted-foreground text-xs">Tool Servers</div>
        <div className="flex flex-wrap gap-2">
            {agent.agent_config.tool_servers.map((name: string) => (
                <Badge key={name} variant="outline" className="border-primary/30 text-primary">
                    {name}
                </Badge>
            ))}
        </div>
    </div>
)}
```

### Step 5: Update AgentDetailPage edit form

Add the same tool servers multi-select pattern from Step 2 to the edit form in `AgentDetailPage.tsx`:

1. Import `useToolServers` from the tool-servers feature
2. Fetch active tool servers: `const { data: toolServers = [] } = useToolServers('active');`
3. Add `tool_servers` to the edit form's Zod schema (same as Step 1)
4. Add the checkbox list between Tools and Human-in-the-Loop (same pattern as Step 2)
5. Initialize the form with the agent's current `tool_servers` value:
   ```typescript
   useEffect(() => {
       if (agent) {
           form.reset({
               // ... existing fields ...
               tool_servers: agent.agent_config?.tool_servers || [],
           });
       }
   }, [agent, form]);
   ```
6. Include `tool_servers` in the update request payload (same pattern as Step 3)

### Step 6: Update task detail view

In the task detail component (`services/console/src/features/task-detail/TaskDetailPage.tsx`), if the snapshotted agent config displays `allowed_tools`, add a similar display for `tool_servers`:

```typescript
{/* Tool Servers (from snapshotted config) */}
{task?.agent_config?.tool_servers && task.agent_config.tool_servers.length > 0 && (
    <div className="space-y-1">
        <div className="uppercase tracking-widest text-muted-foreground text-xs">Tool Servers</div>
        <div className="flex flex-wrap gap-1">
            {task.agent_config.tool_servers.map((name: string) => (
                <Badge key={name} variant="outline" className="border-primary/30 text-primary text-xs">
                    {name}
                </Badge>
            ))}
        </div>
    </div>
)}
```

**Note:** The task detail page may not directly expose `agent_config` — check the actual data structure. If the snapshotted config is nested under a different key, adjust accordingly. If the task detail doesn't show agent config details at all, skip this step.

## Acceptance Criteria

- [ ] `CreateAgentDialog` has a "Tool Servers" section with multi-select checkboxes
- [ ] Each checkbox shows server name and URL
- [ ] Empty tool server list shows hint text
- [ ] Selected tool servers are included in the create request as `agent_config.tool_servers`
- [ ] `AgentDetailPage` read-only view shows assigned tool servers as badges
- [ ] `AgentDetailPage` edit form has the same tool servers multi-select
- [ ] Edit form initializes with the agent's current `tool_servers` values
- [ ] Updated tool servers are included in the update request
- [ ] Task detail view shows snapshotted `tool_servers` (if agent config is displayed)
- [ ] Default value for `tool_servers` is empty array (no servers selected)
- [ ] Existing agent creation without tool servers still works (backward compatible)

## Testing Requirements

- **Browser verification:** After `make start`:
  1. Create an agent with tool servers selected → verify `tool_servers` in agent config
  2. View agent detail → verify tool servers shown in read-only view
  3. Edit agent → verify tool servers pre-selected and updatable
  4. Create agent without tool servers → verify backward compatibility
- **Unit tests:** (If existing component tests) Form schema validates `tool_servers` as optional string array.

## Constraints and Guardrails

- Do not modify the tool server multi-select to be a dropdown or combobox — use checkboxes to match the existing built-in tools pattern.
- Do not add tool server CRUD functionality to the agent pages — that belongs in the Tool Servers feature (Task 6).
- Do not add tool discovery to the agent editor — discovery is available on the tool server detail page.
- Use the existing `useToolServers('active')` hook — do not create a separate hook for this.

## Assumptions

- Task 2 has been completed (Tool Server list API returns active servers).
- Task 3 has been completed (`AgentConfig` TypeScript type includes optional `tool_servers`).
- Task 6 has been completed (`useToolServers()` hook exists and works).
- The `Badge` component from shadcn/ui is available for displaying server names in read-only view.
- The existing agent create/update mutation hooks handle `tool_servers` in the payload (no API client changes needed — the field is part of `agent_config` which is sent as-is).

<!-- AGENT_TASK_END: task-7-console-agent-config.md -->
