<!-- AGENT_TASK_START: task-6-console-tool-servers.md -->

# Task 6 — Console: Tool Servers Management Area

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/phase-2/track-4-custom-tool-runtime.md` — canonical design contract (Console Design section)
2. `services/console/src/features/agents/AgentsListPage.tsx` — list page pattern
3. `services/console/src/features/agents/AgentDetailPage.tsx` — detail page pattern
4. `services/console/src/features/agents/CreateAgentDialog.tsx` — dialog/form pattern
5. `services/console/src/features/agents/useAgents.ts` — React Query hooks pattern
6. `services/console/src/api/client.ts` — API client pattern
7. `services/console/src/layout/Sidebar.tsx` — navigation items
8. `services/console/src/App.tsx` — router configuration
9. `services/console/src/types/index.ts` — TypeScript type definitions

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/phase-2/track-4/progress.md` to "Done".

## Context

Track 4 adds a Tool Servers management area to the Console. This includes a list page showing all registered servers, a detail page with server info and tool discovery, and a dialog for registering new servers. The UI follows the same patterns established by the Agents feature.

## Task-Specific Shared Contract

- API endpoints: `GET /v1/tool-servers`, `GET /v1/tool-servers/:id`, `POST /v1/tool-servers`, `PUT /v1/tool-servers/:id`, `DELETE /v1/tool-servers/:id`, `POST /v1/tool-servers/:id/discover`
- `auth_token` is never shown in full in the UI (masked in detail view, omitted from list)
- Status values: `active` (green badge), `disabled` (gray badge)
- Discover shows tool list in a table after clicking "Discover Tools" button
- Follow existing Console styling: dark theme, `console-surface` classes, `rounded-[28px]`, shadcn/ui components

## Affected Component

- **Service/Module:** Console — Tool Servers Feature
- **File paths:**
  - `services/console/src/types/index.ts` (modify — add tool server types)
  - `services/console/src/api/client.ts` (modify — add tool server API methods)
  - `services/console/src/features/tool-servers/useToolServers.ts` (new — React Query hooks)
  - `services/console/src/features/tool-servers/ToolServersListPage.tsx` (new — list page)
  - `services/console/src/features/tool-servers/ToolServerDetailPage.tsx` (new — detail page)
  - `services/console/src/features/tool-servers/RegisterToolServerDialog.tsx` (new — register dialog)
  - `services/console/src/layout/Sidebar.tsx` (modify — add nav item)
  - `services/console/src/App.tsx` (modify — add routes)
- **Change type:** new code + modifications

## Dependencies

- **Must complete first:** Task 2 (Tool Server API — endpoints must exist)
- **Provides output to:** Task 8 (Integration Tests — console tests)
- **Shared interfaces/contracts:** Tool Server API contract from Task 2

## Implementation Specification

### Step 1: Add TypeScript types

Add to `services/console/src/types/index.ts`:

```typescript
// Tool Server types
export interface ToolServerSummaryResponse {
    server_id: string;
    tenant_id: string;
    name: string;
    url: string;
    auth_type: 'none' | 'bearer_token';
    status: 'active' | 'disabled';
    created_at: string;
    updated_at: string;
}

export interface ToolServerResponse {
    server_id: string;
    tenant_id: string;
    name: string;
    url: string;
    auth_type: 'none' | 'bearer_token';
    auth_token: string | null;
    status: 'active' | 'disabled';
    created_at: string;
    updated_at: string;
}

export interface ToolServerCreateRequest {
    name: string;
    url: string;
    auth_type: 'none' | 'bearer_token';
    auth_token?: string;
}

export interface ToolServerUpdateRequest {
    name?: string;
    url?: string;
    auth_type?: 'none' | 'bearer_token';
    auth_token?: string;
    status?: 'active' | 'disabled';
}

export interface DiscoveredToolInfo {
    name: string;
    description: string;
    input_schema: Record<string, unknown> | null;
}

export interface ToolDiscoverResponse {
    server_id: string;
    server_name: string;
    status: 'reachable' | 'unreachable';
    error: string | null;
    tools: DiscoveredToolInfo[];
}
```

### Step 2: Add API client methods

Add to the `api` object in `services/console/src/api/client.ts`:

```typescript
// Tool Servers
createToolServer: (request: ToolServerCreateRequest) =>
    fetchApi<ToolServerResponse>('/v1/tool-servers', {
        method: 'POST',
        body: JSON.stringify(request),
    }),

listToolServers: (status?: string) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    const query = params.toString();
    return fetchApi<ToolServerSummaryResponse[]>(`/v1/tool-servers${query ? '?' + query : ''}`);
},

getToolServer: (serverId: string) =>
    fetchApi<ToolServerResponse>(`/v1/tool-servers/${encodeURIComponent(serverId)}`),

updateToolServer: (serverId: string, request: ToolServerUpdateRequest) =>
    fetchApi<ToolServerResponse>(`/v1/tool-servers/${encodeURIComponent(serverId)}`, {
        method: 'PUT',
        body: JSON.stringify(request),
    }),

deleteToolServer: (serverId: string) =>
    fetchApi<void>(`/v1/tool-servers/${encodeURIComponent(serverId)}`, {
        method: 'DELETE',
    }),

discoverToolServer: (serverId: string) =>
    fetchApi<ToolDiscoverResponse>(`/v1/tool-servers/${encodeURIComponent(serverId)}/discover`, {
        method: 'POST',
    }),
```

Import the new types at the top of `client.ts`.

### Step 3: Create React Query hooks

Create `services/console/src/features/tool-servers/useToolServers.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { ToolServerCreateRequest, ToolServerUpdateRequest } from '../../types';

const TOOL_SERVERS_KEY = ['tool-servers'];

export function useToolServers(status?: string) {
    return useQuery({
        queryKey: [...TOOL_SERVERS_KEY, status],
        queryFn: () => api.listToolServers(status),
    });
}

export function useToolServer(serverId: string) {
    return useQuery({
        queryKey: ['tool-server', serverId],
        queryFn: () => api.getToolServer(serverId),
        enabled: !!serverId,
        staleTime: 30_000,
    });
}

export function useCreateToolServer() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (request: ToolServerCreateRequest) => api.createToolServer(request),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: TOOL_SERVERS_KEY });
        },
    });
}

export function useUpdateToolServer() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: ({ serverId, request }: { serverId: string; request: ToolServerUpdateRequest }) =>
            api.updateToolServer(serverId, request),
        onSuccess: (_data, variables) => {
            queryClient.invalidateQueries({ queryKey: TOOL_SERVERS_KEY });
            queryClient.invalidateQueries({ queryKey: ['tool-server', variables.serverId] });
        },
    });
}

export function useDeleteToolServer() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (serverId: string) => api.deleteToolServer(serverId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: TOOL_SERVERS_KEY });
        },
    });
}

export function useDiscoverToolServer() {
    return useMutation({
        mutationFn: (serverId: string) => api.discoverToolServer(serverId),
    });
}
```

### Step 4: Create ToolServersListPage

Create `services/console/src/features/tool-servers/ToolServersListPage.tsx`:

Follow the `AgentsListPage.tsx` pattern exactly:

- Header section with title ("Tool Servers"), icon (`Server` from lucide-react), and "Register Tool Server" button
- Status filter dropdown (All / Active / Disabled)
- Table with columns: Name, URL, Auth Type, Status, Created
- Status badge: `active` → green, `disabled` → gray
- Row click navigates to `/tool-servers/:serverId`
- Empty state: "No tool servers registered. Register one to give your agents custom tools."
- Loading state with spinner
- `RegisterToolServerDialog` triggered by the register button

```typescript
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Server, Plus } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../../components/ui/table';
import { Badge } from '../../components/ui/badge';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../../components/ui/select';
import { useToolServers } from './useToolServers';
import { RegisterToolServerDialog } from './RegisterToolServerDialog';

export function ToolServersListPage() {
    const [status, setStatus] = useState('');
    const [dialogOpen, setDialogOpen] = useState(false);
    const navigate = useNavigate();
    const { data: servers = [], isLoading } = useToolServers(status || undefined);

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            {/* Header */}
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7">
                <div className="flex items-center justify-between flex-wrap gap-4">
                    <div>
                        <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2">
                            <Server className="w-6 h-6 text-primary" />
                            Tool Servers
                        </h2>
                        <p className="text-muted-foreground text-sm">
                            External MCP tool servers that agents can use for custom tool capabilities.
                        </p>
                    </div>
                    <div className="flex items-center gap-3">
                        <Select value={status} onValueChange={setStatus}>
                            <SelectTrigger className="w-[140px] rounded-none border-border bg-black/50">
                                <SelectValue placeholder="All statuses" />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="">All</SelectItem>
                                <SelectItem value="active">Active</SelectItem>
                                <SelectItem value="disabled">Disabled</SelectItem>
                            </SelectContent>
                        </Select>
                        <Button
                            onClick={() => setDialogOpen(true)}
                            className="rounded-none bg-primary hover:bg-primary/90"
                        >
                            <Plus className="w-4 h-4 mr-2" />
                            Register Tool Server
                        </Button>
                    </div>
                </div>
            </div>

            {/* Table */}
            <div className="console-surface rounded-[28px] overflow-hidden">
                <Table>
                    <TableHeader className="sticky top-0 z-10">
                        <TableRow className="border-white/8 hover:bg-transparent">
                            <TableHead className="text-muted-foreground">Name</TableHead>
                            <TableHead className="text-muted-foreground">URL</TableHead>
                            <TableHead className="text-muted-foreground">Auth</TableHead>
                            <TableHead className="text-muted-foreground">Status</TableHead>
                            <TableHead className="text-muted-foreground">Created</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {isLoading ? (
                            <TableRow>
                                <TableCell colSpan={5} className="text-center py-12 text-muted-foreground">
                                    Loading...
                                </TableCell>
                            </TableRow>
                        ) : servers.length === 0 ? (
                            <TableRow>
                                <TableCell colSpan={5} className="text-center py-12 text-muted-foreground">
                                    No tool servers registered. Register one to give your agents custom tools.
                                </TableCell>
                            </TableRow>
                        ) : (
                            servers.map((server) => (
                                <TableRow
                                    key={server.server_id}
                                    className="border-white/8 cursor-pointer hover:bg-white/5"
                                    onClick={() => navigate(`/tool-servers/${server.server_id}`)}
                                >
                                    <TableCell className="font-medium">{server.name}</TableCell>
                                    <TableCell className="text-muted-foreground font-mono text-xs truncate max-w-[300px]">
                                        {server.url}
                                    </TableCell>
                                    <TableCell className="text-muted-foreground text-xs">
                                        {server.auth_type === 'bearer_token' ? 'Bearer Token' : 'None'}
                                    </TableCell>
                                    <TableCell>
                                        <Badge variant={server.status === 'active' ? 'default' : 'secondary'}
                                               className={server.status === 'active' ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30' : ''}>
                                            {server.status}
                                        </Badge>
                                    </TableCell>
                                    <TableCell className="text-muted-foreground text-xs">
                                        {new Date(server.created_at).toLocaleDateString()}
                                    </TableCell>
                                </TableRow>
                            ))
                        )}
                    </TableBody>
                </Table>
            </div>

            <RegisterToolServerDialog open={dialogOpen} onOpenChange={setDialogOpen} />
        </div>
    );
}
```

### Step 5: Create RegisterToolServerDialog

Create `services/console/src/features/tool-servers/RegisterToolServerDialog.tsx`:

Follow the `CreateAgentDialog.tsx` pattern:

```typescript
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { toast } from 'sonner';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../../components/ui/dialog';
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '../../components/ui/form';
import { Input } from '../../components/ui/input';
import { Button } from '../../components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../../components/ui/select';
import { useCreateToolServer } from './useToolServers';

const registerSchema = z.object({
    name: z.string()
        .min(1, 'Name is required')
        .max(100)
        .regex(/^[a-z0-9][a-z0-9-]*$/, 'Must be lowercase alphanumeric with hyphens'),
    url: z.string()
        .min(1, 'URL is required')
        .max(2048)
        .url('Must be a valid URL'),
    auth_type: z.enum(['none', 'bearer_token']).default('none'),
    auth_token: z.string().optional(),
}).refine(
    (data) => data.auth_type !== 'bearer_token' || (data.auth_token && data.auth_token.length > 0),
    { message: 'Auth token is required for Bearer Token auth', path: ['auth_token'] }
);

type RegisterFormValues = z.infer<typeof registerSchema>;

interface RegisterToolServerDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
}

export function RegisterToolServerDialog({ open, onOpenChange }: RegisterToolServerDialogProps) {
    const mutation = useCreateToolServer();
    const form = useForm<RegisterFormValues>({
        resolver: zodResolver(registerSchema),
        defaultValues: {
            name: '',
            url: '',
            auth_type: 'none',
            auth_token: '',
        },
    });

    const authType = form.watch('auth_type');

    function onSubmit(data: RegisterFormValues) {
        mutation.mutate(
            {
                name: data.name,
                url: data.url,
                auth_type: data.auth_type,
                auth_token: data.auth_type === 'bearer_token' ? data.auth_token : undefined,
            },
            {
                onSuccess: () => {
                    toast.success('Tool server registered', { description: `${data.name} is now available for agents.` });
                    form.reset();
                    onOpenChange(false);
                },
                onError: (error: Error) => {
                    toast.error('Failed to register tool server', { description: error.message });
                },
            }
        );
    }

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-[500px] console-surface border-white/10">
                <DialogHeader>
                    <DialogTitle>Register Tool Server</DialogTitle>
                </DialogHeader>
                <Form {...form}>
                    <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-5">
                        <FormField control={form.control} name="name" render={({ field }) => (
                            <FormItem>
                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Name</FormLabel>
                                <FormControl>
                                    <Input className="rounded-none border-border bg-black/50" placeholder="e.g., jira-tools" {...field} />
                                </FormControl>
                                <FormMessage className="text-destructive font-bold text-xs" />
                            </FormItem>
                        )} />

                        <FormField control={form.control} name="url" render={({ field }) => (
                            <FormItem>
                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">URL</FormLabel>
                                <FormControl>
                                    <Input className="rounded-none border-border bg-black/50" placeholder="e.g., http://localhost:9000/mcp" {...field} />
                                </FormControl>
                                <FormMessage className="text-destructive font-bold text-xs" />
                            </FormItem>
                        )} />

                        <FormField control={form.control} name="auth_type" render={({ field }) => (
                            <FormItem>
                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Authentication</FormLabel>
                                <Select onValueChange={field.onChange} defaultValue={field.value}>
                                    <FormControl>
                                        <SelectTrigger className="rounded-none border-border bg-black/50">
                                            <SelectValue />
                                        </SelectTrigger>
                                    </FormControl>
                                    <SelectContent>
                                        <SelectItem value="none">None</SelectItem>
                                        <SelectItem value="bearer_token">Bearer Token</SelectItem>
                                    </SelectContent>
                                </Select>
                                <FormMessage className="text-destructive font-bold text-xs" />
                            </FormItem>
                        )} />

                        {authType === 'bearer_token' && (
                            <FormField control={form.control} name="auth_token" render={({ field }) => (
                                <FormItem>
                                    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Auth Token</FormLabel>
                                    <FormControl>
                                        <Input type="password" className="rounded-none border-border bg-black/50" placeholder="Bearer token" {...field} />
                                    </FormControl>
                                    <FormMessage className="text-destructive font-bold text-xs" />
                                </FormItem>
                            )} />
                        )}

                        <div className="flex justify-end gap-3 pt-2">
                            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
                            <Button type="submit" disabled={mutation.isPending} className="rounded-none bg-primary hover:bg-primary/90">
                                {mutation.isPending ? 'Registering...' : 'Register'}
                            </Button>
                        </div>
                    </form>
                </Form>
            </DialogContent>
        </Dialog>
    );
}
```

### Step 6: Create ToolServerDetailPage

Create `services/console/src/features/tool-servers/ToolServerDetailPage.tsx`:

Follow the `AgentDetailPage.tsx` pattern — read-only view with discover and edit/delete actions:

- Server info: name, URL, auth type (token masked), status badge
- "Discover Tools" button → calls discover endpoint → shows tools in a table (name, description)
- Edit button toggles inline editing (URL, auth config, status)
- Delete button with confirmation dialog
- Back navigation to /tool-servers

Key sections:
1. Header with server name, status badge, and action buttons
2. Server details card (URL, auth type, masked token, timestamps)
3. Discovered Tools section (initially empty, populated after clicking "Discover Tools")
4. Edit mode (toggles fields to editable inputs)
5. Delete confirmation dialog

The detail page should use:
- `useToolServer(serverId)` for fetching
- `useUpdateToolServer()` for editing
- `useDeleteToolServer()` for deletion
- `useDiscoverToolServer()` for tool discovery

### Step 7: Add navigation and routes

**Sidebar.tsx** — Add a nav item after "Agents":

```typescript
{ path: '/tool-servers', label: 'Tool Servers', icon: Server, end: true },
```

Import `Server` from `lucide-react`.

**App.tsx** — Add routes inside the `<Route element={<AppShell />}>`:

```typescript
<Route path="/tool-servers" element={<ToolServersListPage />} />
<Route path="/tool-servers/:serverId" element={<ToolServerDetailPage />} />
```

Import the new page components.

## Acceptance Criteria

- [ ] "Tool Servers" appears in the sidebar navigation
- [ ] `/tool-servers` route renders the list page with registered servers
- [ ] List page supports status filter (All / Active / Disabled)
- [ ] "Register Tool Server" button opens a dialog with name, URL, auth type, and token fields
- [ ] Registration validates name pattern, URL format, and token requirement
- [ ] Row click navigates to `/tool-servers/:serverId` detail page
- [ ] Detail page shows server info with masked auth token
- [ ] "Discover Tools" button probes the server and shows discovered tools in a table
- [ ] Edit mode allows updating URL, auth config, and status
- [ ] Delete button removes the server with confirmation
- [ ] Empty state shown when no servers are registered
- [ ] All styling follows existing Console patterns (dark theme, console-surface classes, rounded corners)

## Testing Requirements

- **Unit tests:** (If the console has component tests) List page renders servers, register dialog validates inputs, detail page shows server info.
- **Browser verification:** After `make start`, navigate to `/tool-servers` in browser, verify list page renders, register a server, view detail page, discover tools.

## Constraints and Guardrails

- Follow existing Console patterns exactly — do not introduce new UI libraries or styling approaches.
- Use shadcn/ui components (`Table`, `Badge`, `Dialog`, `Form`, `Input`, `Select`, `Button`) — do not create custom components when shadcn equivalents exist.
- Use React Query hooks for all API calls — do not use `useEffect` + `fetch` directly.
- Do not show auth tokens in plain text anywhere in the UI.
- Do not implement WebSocket or real-time updates — use standard query invalidation.

## Assumptions

- Task 2 has been completed (Tool Server API endpoints exist and respond correctly).
- The shadcn/ui components listed above are already installed in the Console project.
- The `lucide-react` library includes a `Server` icon.
- The `console-surface`, `console-surface-strong`, and other CSS classes are globally available.
- The `toast` import from `sonner` is already configured in the project.

<!-- AGENT_TASK_END: task-6-console-tool-servers.md -->
