<!-- AGENT_TASK_START: task-8-console-file-and-sandbox.md -->

# Task 8 — Console: File Attachment + Sandbox Config

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Sections 4 and 5: console UI changes)
2. `docs/exec-plans/active/agent-capabilities/track-2/plan.md` — Track 2 execution plan
3. `services/console/src/features/submit/SubmitTaskPage.tsx` — existing task submission form
4. `services/console/src/features/submit/useSubmitTask.ts` — existing submission hook
5. `services/console/src/features/submit/schema.ts` — existing validation schema and ALLOWED_TOOLS
6. `services/console/src/features/agents/CreateAgentDialog.tsx` — existing agent creation dialog
7. `services/console/src/api/client.ts` — API client for HTTP requests

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-2/progress.md` to "Done".

## Context

This task adds two console features:

1. **File attachment on task submit** — drag-and-drop/file picker for attaching files to task submissions. Files are sent as multipart/form-data to the API. Only enabled when the selected agent has `sandbox.enabled: true`.

2. **Sandbox config in agent creation** — a new "Sandbox" section in the CreateAgentDialog for configuring sandbox settings (enabled, template, vcpu, memory_mb, timeout_seconds).

Both features extend existing components with additional UI elements.

## Task-Specific Shared Contract

- File attachment only visible/enabled when selected agent has `sandbox.enabled: true`
- File size limits enforced client-side: 50 MB per file, 200 MB total
- When files are present, submit as multipart/form-data instead of JSON
- `FormData` with `task_request` JSON part + `files` binary parts
- Sandbox tools added to `ALLOWED_TOOLS` in schema.ts: `sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `sandbox_download`
- Sandbox config section in agent dialog: toggle for enabled, text field for template, number fields for vcpu/memory_mb/timeout_seconds
- Sandbox section collapsed by default, expanded when enabled toggled on
- Validation matching API rules (vcpu 1-8, memory_mb 512-8192, timeout_seconds 60-86400)

## Affected Component

- **Service/Module:** Console — Submit Task + Agent Creation
- **File paths:**
  - `services/console/src/features/submit/FileAttachment.tsx` (new)
  - `services/console/src/features/submit/SubmitTaskPage.tsx` (modify)
  - `services/console/src/features/submit/useSubmitTask.ts` (modify)
  - `services/console/src/features/submit/schema.ts` (modify)
  - `services/console/src/features/agents/CreateAgentDialog.tsx` (modify)
  - `services/console/src/api/client.ts` (modify)
  - `services/console/src/features/submit/FileAttachment.test.tsx` (new)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 1 (DB migration — sandbox config in agent), Task 6 (API multipart endpoint)
- **Provides output to:** Task 9 (Integration Tests)
- **Shared interfaces/contracts:** API multipart endpoint, agent config sandbox schema

## Implementation Specification

### Step 1: Add sandbox tools to ALLOWED_TOOLS

Modify `services/console/src/features/submit/schema.ts`:

```typescript
// Capability tools — do external work and return results
export const ALLOWED_TOOLS = [
    { id: "web_search", label: "Web Search" },
    { id: "read_url", label: "Read URL" },
    { id: "calculator", label: "Calculator" },
    { id: "sandbox_exec", label: "Sandbox Exec" },
    { id: "sandbox_read_file", label: "Sandbox Read File" },
    { id: "sandbox_write_file", label: "Sandbox Write File" },
    { id: "sandbox_download", label: "Sandbox Download" },
    { id: "upload_artifact", label: "Upload Artifact" },
    ...(devTaskControlsEnabled ? [{ id: "dev_sleep", label: "Dev Sleep" }] : [])
];
```

### Step 2: Create FileAttachment component

Create `services/console/src/features/submit/FileAttachment.tsx`:

```tsx
import { useCallback, useState } from 'react';
import { X, Upload, FileIcon, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';

const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024; // 50 MB
const MAX_TOTAL_SIZE_BYTES = 200 * 1024 * 1024; // 200 MB

interface FileAttachmentProps {
    files: File[];
    onFilesChange: (files: File[]) => void;
    disabled?: boolean;
    disabledReason?: string;
}

function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function validateFiles(files: File[]): string | null {
    for (const file of files) {
        if (file.size > MAX_FILE_SIZE_BYTES) {
            return `File "${file.name}" exceeds the 50 MB limit (${formatFileSize(file.size)})`;
        }
    }
    const totalSize = files.reduce((sum, f) => sum + f.size, 0);
    if (totalSize > MAX_TOTAL_SIZE_BYTES) {
        return `Total file size exceeds the 200 MB limit (${formatFileSize(totalSize)})`;
    }
    return null;
}

export function FileAttachment({ files, onFilesChange, disabled = false, disabledReason }: FileAttachmentProps) {
    const [dragOver, setDragOver] = useState(false);
    const [validationError, setValidationError] = useState<string | null>(null);

    const addFiles = useCallback((newFiles: FileList | File[]) => {
        const fileArray = Array.from(newFiles);
        const combined = [...files, ...fileArray];

        const error = validateFiles(combined);
        if (error) {
            setValidationError(error);
            return;
        }
        setValidationError(null);
        onFilesChange(combined);
    }, [files, onFilesChange]);

    const removeFile = useCallback((index: number) => {
        const updated = files.filter((_, i) => i !== index);
        setValidationError(null);
        onFilesChange(updated);
    }, [files, onFilesChange]);

    const handleDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(false);
        if (disabled) return;
        if (e.dataTransfer.files.length > 0) {
            addFiles(e.dataTransfer.files);
        }
    }, [addFiles, disabled]);

    const handleDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        if (!disabled) setDragOver(true);
    }, [disabled]);

    const handleDragLeave = useCallback(() => {
        setDragOver(false);
    }, []);

    const handleFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files.length > 0) {
            addFiles(e.target.files);
            e.target.value = ''; // Reset input
        }
    }, [addFiles]);

    const totalSize = files.reduce((sum, f) => sum + f.size, 0);

    return (
        <div className="space-y-3">
            {disabled && disabledReason && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <AlertCircle className="w-3.5 h-3.5" />
                    <span>{disabledReason}</span>
                </div>
            )}

            <div
                className={`border-2 border-dashed rounded-lg p-6 text-center transition-colors ${
                    disabled
                        ? 'border-border/30 bg-muted/5 cursor-not-allowed opacity-50'
                        : dragOver
                        ? 'border-primary bg-primary/5'
                        : 'border-border hover:border-primary/50 cursor-pointer'
                }`}
                onDrop={handleDrop}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onClick={() => {
                    if (!disabled) {
                        document.getElementById('file-input')?.click();
                    }
                }}
            >
                <Upload className="w-8 h-8 mx-auto mb-2 text-muted-foreground" />
                <p className="text-sm text-muted-foreground">
                    {disabled
                        ? 'File upload not available'
                        : 'Drop files here or click to browse'}
                </p>
                <p className="text-xs text-muted-foreground/60 mt-1">
                    Max 50 MB per file, 200 MB total
                </p>
                <input
                    id="file-input"
                    type="file"
                    multiple
                    className="hidden"
                    onChange={handleFileInput}
                    disabled={disabled}
                />
            </div>

            {validationError && (
                <div className="flex items-center gap-2 text-xs text-destructive">
                    <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                    <span>{validationError}</span>
                </div>
            )}

            {files.length > 0 && (
                <div className="space-y-2">
                    {files.map((file, index) => (
                        <div
                            key={`${file.name}-${index}`}
                            className="flex items-center gap-3 p-2 rounded bg-muted/10 border border-white/5"
                        >
                            <FileIcon className="w-4 h-4 text-muted-foreground shrink-0" />
                            <div className="flex-1 min-w-0">
                                <p className="text-sm font-mono truncate">{file.name}</p>
                                <p className="text-xs text-muted-foreground">
                                    {formatFileSize(file.size)}
                                </p>
                            </div>
                            <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-6 w-6 p-0 hover:bg-destructive/20 hover:text-destructive"
                                onClick={(e) => {
                                    e.stopPropagation();
                                    removeFile(index);
                                }}
                            >
                                <X className="w-3.5 h-3.5" />
                            </Button>
                        </div>
                    ))}
                    <p className="text-xs text-muted-foreground">
                        {files.length} file{files.length !== 1 ? 's' : ''} ({formatFileSize(totalSize)})
                    </p>
                </div>
            )}
        </div>
    );
}
```

### Step 3: Add multipart submit to API client

Modify `services/console/src/api/client.ts` to add a multipart submit method:

The `api` object in `client.ts` is a plain object (not a class), and uses a `fetchApi` helper for all requests. The `fetchApi` helper auto-sets `Content-Type: application/json` when a body is present, which must NOT happen for multipart. For multipart, pass the `FormData` body and explicitly avoid setting `Content-Type` (the browser sets it with the boundary).

Add this method to the `api` object:

```typescript
    submitTaskMultipart: (request: TaskSubmissionRequest, files: File[]) => {
        const formData = new FormData();
        formData.append('task_request', JSON.stringify({
            agent_id: request.agent_id,
            input: request.input,
            max_steps: request.max_steps,
            max_retries: request.max_retries,
            task_timeout_seconds: request.task_timeout_seconds,
            langfuse_endpoint_id: request.langfuse_endpoint_id,
        }));
        for (const file of files) {
            formData.append('files', file);
        }
        // Use fetchApi but without Content-Type header (browser sets multipart boundary).
        // fetchApi only auto-sets Content-Type when options.body is present AND no
        // Content-Type header exists — but FormData should NOT have Content-Type set
        // manually. We pass an empty Headers to prevent the auto-set.
        return fetchApi<TaskSubmissionResponse>('/v1/tasks', {
            method: 'POST',
            body: formData,
            headers: {},  // Prevent fetchApi from setting Content-Type; browser sets it with boundary
        });
    },
```

**Note:** The existing `fetchApi` helper checks `!headers.has('Content-Type')` before auto-setting it. Since we pass `headers: {}` (a plain object, not a `Headers` instance), the `new Headers(options?.headers)` call in `fetchApi` creates an empty `Headers` — which does NOT have `Content-Type`, so `fetchApi` would try to set it. To prevent this for `FormData`, you may need a small guard in `fetchApi`:

```typescript
// In fetchApi, update the Content-Type auto-set logic:
if (!headers.has('Content-Type') && options?.method !== 'GET' && options?.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
}
```

This ensures `fetchApi` skips auto-setting `Content-Type` when the body is `FormData`, letting the browser set the correct multipart boundary.

### Step 4: Update useSubmitTask hook

Modify `services/console/src/features/submit/useSubmitTask.ts`:

```typescript
import { useMutation } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskSubmissionRequest } from '@/types';

interface SubmitTaskInput {
    request: TaskSubmissionRequest;
    files?: File[];
}

export function useSubmitTask() {
    return useMutation({
        mutationFn: ({ request, files }: SubmitTaskInput) => {
            if (files && files.length > 0) {
                return api.submitTaskMultipart(request, files);
            }
            return api.submitTask(request);
        },
    });
}
```

### Step 5: Integrate FileAttachment into SubmitTaskPage

Modify `services/console/src/features/submit/SubmitTaskPage.tsx`:

Add imports:
```typescript
import { FileAttachment } from './FileAttachment';
```

Add state for files:
```typescript
const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
```

Determine if sandbox is enabled for the selected agent:
```typescript
const sandboxEnabled = selectedAgent?.agent_config?.sandbox?.enabled === true;
```

Update the `onSubmit` function to handle files:
```typescript
    function onSubmit(data: SubmitTaskFormValues) {
        mutation.mutate(
            {
                request: data,
                files: attachedFiles.length > 0 ? attachedFiles : undefined,
            },
            {
                onSuccess: (response) => {
                    toast.success(`Task ${response.task_id} submitted`, {
                        description: attachedFiles.length > 0
                            ? `Execution initialized with ${attachedFiles.length} file(s).`
                            : "Execution initialized.",
                    });
                    setAttachedFiles([]);
                    navigate(`/tasks/${response.task_id}`);
                },
                onError: (error: Error) => {
                    toast.error("Submission failed", {
                        description: error.message || "Unknown error occurred.",
                    });
                },
            }
        );
    }
```

Add the FileAttachment component after the Task Input card:
```tsx
                        {/* File Attachments */}
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-3">
                                <CardTitle className="text-sm font-display uppercase tracking-widest">File Attachments</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-4">
                                <FileAttachment
                                    files={attachedFiles}
                                    onFilesChange={setAttachedFiles}
                                    disabled={!sandboxEnabled}
                                    disabledReason={
                                        !selectedAgentId
                                            ? "Select an agent first"
                                            : !sandboxEnabled
                                            ? "File upload requires an agent with sandbox enabled"
                                            : undefined
                                    }
                                />
                            </CardContent>
                        </Card>
```

### Step 6: Add sandbox config to CreateAgentDialog

Modify `services/console/src/features/agents/CreateAgentDialog.tsx`:

Update the schema:
```typescript
const createAgentSchema = z.object({
    display_name: z.string().min(1, 'Agent name is required').max(200),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).default(0.7),
    allowed_tools: z.array(z.string()).default([]),
    tool_servers: z.array(z.string()).default([]),
    max_concurrent_tasks: z.number().int().min(1).default(5),
    budget_max_per_task: z.number().int().min(1).default(500000),
    budget_max_per_hour: z.number().int().min(1).default(5000000),
    sandbox_enabled: z.boolean().default(false),
    sandbox_template: z.string().default(''),
    sandbox_vcpu: z.number().int().min(1).max(8).default(2),
    sandbox_memory_mb: z.number().int().min(512).max(8192).default(2048),
    sandbox_timeout_seconds: z.number().int().min(60).max(86400).default(3600),
});
```

Add default values:
```typescript
        defaultValues: {
            // ... existing defaults ...
            sandbox_enabled: false,
            sandbox_template: '',
            sandbox_vcpu: 2,
            sandbox_memory_mb: 2048,
            sandbox_timeout_seconds: 3600,
        },
```

Watch sandbox_enabled for conditional rendering:
```typescript
    const sandboxEnabled = form.watch('sandbox_enabled');
```

Update onSubmit to include sandbox config:
```typescript
    function onSubmit(data: CreateAgentFormValues) {
        const sandboxConfig = data.sandbox_enabled
            ? {
                enabled: true,
                template: data.sandbox_template,
                vcpu: data.sandbox_vcpu,
                memory_mb: data.sandbox_memory_mb,
                timeout_seconds: data.sandbox_timeout_seconds,
            }
            : undefined;

        mutation.mutate(
            {
                display_name: data.display_name,
                agent_config: {
                    system_prompt: data.system_prompt,
                    provider: data.provider,
                    model: data.model,
                    temperature: data.temperature,
                    allowed_tools: data.allowed_tools,
                    tool_servers: data.tool_servers,
                    ...(sandboxConfig ? { sandbox: sandboxConfig } : {}),
                },
                max_concurrent_tasks: data.max_concurrent_tasks,
                budget_max_per_task: data.budget_max_per_task,
                budget_max_per_hour: data.budget_max_per_hour,
            },
            // ... existing callbacks ...
        );
    }
```

Add the Sandbox section UI before the Scheduling & Budget section:
```tsx
                        <div className="space-y-3">
                            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">Sandbox (Code Execution)</span>
                            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                                <FormField
                                    control={form.control}
                                    name="sandbox_enabled"
                                    render={({ field }) => (
                                        <FormItem className="flex flex-row items-center gap-3">
                                            <FormControl>
                                                <Checkbox
                                                    className="rounded-none border-primary data-[state=checked]:bg-primary data-[state=checked]:text-black"
                                                    checked={field.value}
                                                    onCheckedChange={field.onChange}
                                                />
                                            </FormControl>
                                            <div>
                                                <FormLabel className="font-normal font-mono cursor-pointer text-sm">
                                                    Enable Sandbox
                                                </FormLabel>
                                                <p className="text-xs text-muted-foreground mt-0.5">
                                                    Provision an E2B sandbox for code execution. Required for file input.
                                                </p>
                                            </div>
                                        </FormItem>
                                    )}
                                />

                                {sandboxEnabled && (
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                                        <FormField
                                            control={form.control}
                                            name="sandbox_template"
                                            render={({ field }) => (
                                                <FormItem className="md:col-span-2">
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Template</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            placeholder="e.g., python-3.11"
                                                            {...field}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="sandbox_vcpu"
                                            render={({ field }) => (
                                                <FormItem>
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">vCPU (1-8)</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="1"
                                                            max="8"
                                                            step="1"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            {...field}
                                                            onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 1)}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="sandbox_memory_mb"
                                            render={({ field }) => (
                                                <FormItem>
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Memory MB (512-8192)</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="512"
                                                            max="8192"
                                                            step="256"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            {...field}
                                                            onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 512)}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="sandbox_timeout_seconds"
                                            render={({ field }) => (
                                                <FormItem className="md:col-span-2">
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Timeout (seconds, 60-86400)</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="60"
                                                            max="86400"
                                                            step="60"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            {...field}
                                                            onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 60)}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                    </div>
                                )}
                            </div>
                        </div>
```

### Step 7: Write unit tests for FileAttachment validation

Create `services/console/src/features/submit/FileAttachment.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest';
import { validateFiles } from './FileAttachment';

describe('validateFiles', () => {
    it('returns null for empty file list', () => {
        expect(validateFiles([])).toBeNull();
    });

    it('returns null for files within limits', () => {
        const files = [
            new File(['x'.repeat(1000)], 'small.txt'),
        ];
        expect(validateFiles(files)).toBeNull();
    });

    it('returns error for file exceeding 50 MB', () => {
        const largeContent = new Uint8Array(50 * 1024 * 1024 + 1);
        const files = [new File([largeContent], 'large.bin')];
        const error = validateFiles(files);
        expect(error).not.toBeNull();
        expect(error).toContain('50 MB');
    });

    it('returns error for total size exceeding 200 MB', () => {
        const files = [
            new File([new Uint8Array(49 * 1024 * 1024)], 'file1.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file2.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file3.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file4.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file5.bin'),
        ];
        const error = validateFiles(files);
        expect(error).not.toBeNull();
        expect(error).toContain('200 MB');
    });

    it('returns null for exactly 50 MB file', () => {
        const files = [
            new File([new Uint8Array(50 * 1024 * 1024)], 'exact.bin'),
        ];
        expect(validateFiles(files)).toBeNull();
    });
});
```

## Acceptance Criteria

- [ ] `schema.ts` ALLOWED_TOOLS includes sandbox_exec, sandbox_read_file, sandbox_write_file, sandbox_download, upload_artifact
- [ ] `FileAttachment.tsx` component created with drag-and-drop and file picker
- [ ] File list shows name, size, and remove button per file
- [ ] File validation: max 50 MB per file, max 200 MB total
- [ ] File attachment disabled when selected agent does not have sandbox.enabled
- [ ] Disabled state shows tooltip/message about sandbox requirement
- [ ] When files present: submit as multipart/form-data via `submitTaskMultipart()`
- [ ] When no files: submit as JSON via existing `submitTask()` (backward compatible)
- [ ] `useSubmitTask` hook accepts optional `files` parameter
- [ ] API client has `submitTaskMultipart()` method using FormData and the existing `fetchApi` helper
- [ ] `fetchApi` updated to skip auto-setting `Content-Type` when body is `FormData`
- [ ] CreateAgentDialog has "Sandbox" section with enabled toggle
- [ ] Sandbox config fields (template, vcpu, memory_mb, timeout_seconds) shown only when enabled
- [ ] Sandbox config defaults: vcpu=2, memory_mb=2048, timeout_seconds=3600
- [ ] Sandbox config validation: vcpu 1-8, memory_mb 512-8192, timeout_seconds 60-86400
- [ ] Sandbox config included in agent_config when creating agent
- [ ] All unit tests pass for file validation
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** File validation — empty list valid, within limits valid, per-file limit exceeded, total limit exceeded, boundary cases.
- **Browser verification:** After `make start`, verify file attachment appears on submit page, disabled without sandbox agent, enabled with sandbox agent. Verify sandbox config section in agent creation dialog.
- **Regression:** `make test` — all existing console tests pass.

## Constraints and Guardrails

- Do not modify the existing JSON submission path — it must remain the default when no files are attached.
- Do not set `Content-Type` header manually for multipart requests — the browser sets it with the boundary.
- Sandbox config section should not be shown by default — only expanded when "Enable Sandbox" is toggled on.
- File attachment area should be visually clear about the sandbox requirement when disabled.
- Do not implement file preview or content inspection — just show name and size.

## Assumptions

- Task 1 has been completed (sandbox config is accepted by the API).
- Task 6 has been completed (multipart endpoint exists at `POST /v1/tasks`).
- The `agent_config` object from the API includes `sandbox` when present (optional field).
- `selectedAgent?.agent_config?.sandbox?.enabled` correctly checks if sandbox is enabled for the selected agent.
- The existing `useAgent()` hook returns agent data including the `agent_config` with the `sandbox` block.
- Vitest is used for console unit tests.

<!-- AGENT_TASK_END: task-8-console-file-and-sandbox.md -->
