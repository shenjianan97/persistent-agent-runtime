<!-- AGENT_TASK_START: task-7-console-artifacts-tab.md -->

# Task 7 — Console Artifacts Tab

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 5: Console UI Changes — Task detail view)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `services/console/src/features/task-detail/TaskDetailPage.tsx` — existing task detail page
4. `services/console/src/api/client.ts` — API client with `fetchApi()` helper
5. `services/console/src/types/index.ts` — TypeScript type definitions
6. `services/console/src/features/submit/schema.ts` — `ALLOWED_TOOLS` list

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 adds an "Artifacts" section to the task detail page in the Console. When a task has artifacts (produced via `upload_artifact`), users can see a list of artifacts with metadata and download individual files.

The artifacts section shows below the output card on the task detail page, only when artifacts exist.

## Task-Specific Shared Contract

- API endpoint: `GET /v1/tasks/{taskId}/artifacts` — returns `ArtifactMetadata[]`
- Download URL: `GET /v1/tasks/{taskId}/artifacts/{filename}` — streams file (browser download)
- Artifact metadata fields: `artifact_id`, `task_id`, `filename`, `direction`, `content_type`, `size_bytes`, `created_at`
- `upload_artifact` must be added to the `ALLOWED_TOOLS` list in `schema.ts` so agents can be configured with it
- File sizes displayed in human-readable format (KB, MB)
- Download triggers via `window.open()` to the download endpoint URL

## Affected Component

- **Service/Module:** Console — Task Detail and API
- **File paths:**
  - `services/console/src/features/task-detail/ArtifactsTab.tsx` (new — artifacts list component)
  - `services/console/src/features/task-detail/useArtifacts.ts` (new — react-query hook)
  - `services/console/src/features/task-detail/TaskDetailPage.tsx` (modify — add artifacts section)
  - `services/console/src/api/client.ts` (modify — add artifact API methods)
  - `services/console/src/types/index.ts` (modify — add artifact type definitions)
  - `services/console/src/features/submit/schema.ts` (modify — add `upload_artifact` to ALLOWED_TOOLS)
- **Change type:** new code + modification

## Dependencies

- **Must complete first:** Task 5 (API Artifact Endpoints — the API must be available)
- **Provides output to:** Task 8 (Integration Tests — Console is the user-facing layer)
- **Shared interfaces/contracts:** API response types, artifact list/download endpoint URLs

## Implementation Specification

### Step 1: Add artifact type definitions

Add the following to `services/console/src/types/index.ts` at the end of the file:

```typescript
// Artifact types
export interface ArtifactMetadata {
    artifactId: string;
    taskId: string;
    filename: string;
    direction: 'input' | 'output';
    contentType: string;
    sizeBytes: number;
    createdAt: string;
}
```

### Step 2: Add artifact API methods to client

Add the following methods to the `api` object in `services/console/src/api/client.ts`:

```typescript
    // Artifacts
    listArtifacts: (taskId: string, direction?: string) => {
        const params = new URLSearchParams();
        if (direction) params.append('direction', direction);
        const query = params.toString();
        return fetchApi<ArtifactMetadata[]>(
            `/v1/tasks/${taskId}/artifacts${query ? `?${query}` : ''}`
        );
    },

    getArtifactDownloadUrl: (taskId: string, filename: string, direction: string = 'output') => {
        const baseUrl = getApiBaseUrl();
        const params = new URLSearchParams({ direction });
        return `${baseUrl}/v1/tasks/${taskId}/artifacts/${encodeURIComponent(filename)}?${params}`;
    },
```

Add `ArtifactMetadata` to the import from `@/types` at the top of the file:

```typescript
import {
    // ... existing imports ...
    ArtifactMetadata,
} from '@/types';
```

### Step 3: Create useArtifacts hook

Create `services/console/src/features/task-detail/useArtifacts.ts`:

```typescript
import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useTaskArtifacts(taskId: string, enabled: boolean = true) {
    return useQuery({
        queryKey: ['task-artifacts', taskId],
        queryFn: () => api.listArtifacts(taskId),
        enabled: !!taskId && enabled,
    });
}
```

### Step 4: Create ArtifactsTab component

Create `services/console/src/features/task-detail/ArtifactsTab.tsx`:

```tsx
import { Download, FileText } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { api } from '@/api/client';
import { ArtifactMetadata } from '@/types';

function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const k = 1024;
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    const size = bytes / Math.pow(k, i);
    return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

interface ArtifactsTabProps {
    taskId: string;
    artifacts: ArtifactMetadata[];
}

export function ArtifactsTab({ taskId, artifacts }: ArtifactsTabProps) {
    if (artifacts.length === 0) {
        return null;
    }

    const handleDownload = (filename: string, direction: string) => {
        const url = api.getArtifactDownloadUrl(taskId, filename, direction);
        window.open(url, '_blank');
    };

    return (
        <Card className="console-surface border-white/10">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-white/8">
                <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-muted-foreground">
                    <FileText className="w-4 h-4" /> Artifacts ({artifacts.length})
                </CardTitle>
            </CardHeader>
            <CardContent className="pt-4">
                <div className="space-y-2">
                    {artifacts.map((artifact) => (
                        <div
                            key={artifact.artifactId}
                            className="flex items-center justify-between gap-4 px-4 py-3 rounded-lg bg-black/20 border border-white/5 hover:border-white/10 transition-colors"
                        >
                            <div className="flex items-center gap-3 min-w-0">
                                <FileText className="w-4 h-4 text-muted-foreground shrink-0" />
                                <div className="min-w-0">
                                    <p className="text-sm font-mono text-foreground truncate">
                                        {artifact.filename}
                                    </p>
                                    <div className="flex gap-3 text-xs text-muted-foreground font-mono uppercase tracking-wider">
                                        <span>{artifact.direction}</span>
                                        <span>{artifact.contentType}</span>
                                        <span>{formatFileSize(artifact.sizeBytes)}</span>
                                        <span>{new Date(artifact.createdAt).toLocaleString()}</span>
                                    </div>
                                </div>
                            </div>
                            <Button
                                variant="ghost"
                                size="sm"
                                className="shrink-0 uppercase tracking-widest text-xs"
                                onClick={() => handleDownload(artifact.filename, artifact.direction)}
                            >
                                <Download className="w-4 h-4 mr-1" /> Download
                            </Button>
                        </div>
                    ))}
                </div>
            </CardContent>
        </Card>
    );
}
```

### Step 5: Add ArtifactsTab to TaskDetailPage

Modify `services/console/src/features/task-detail/TaskDetailPage.tsx`:

Add imports at the top of the file:

```typescript
import { ArtifactsTab } from './ArtifactsTab';
import { useTaskArtifacts } from './useArtifacts';
```

Add the artifacts query after the existing `useCheckpoints` call:

```typescript
    const { data: artifactsData } = useTaskArtifacts(taskId!, !!task);
```

Add the `ArtifactsTab` component in the JSX, after the output card section (the `{task.status === 'completed' && !!task.output && ( ... )}` block) and before the follow-up section:

```tsx
                    {artifactsData && artifactsData.length > 0 && (
                        <ArtifactsTab taskId={taskId!} artifacts={artifactsData} />
                    )}
```

### Step 6: Add upload_artifact to ALLOWED_TOOLS

Add `upload_artifact` to the `ALLOWED_TOOLS` array in `services/console/src/features/submit/schema.ts`:

```typescript
export const ALLOWED_TOOLS = [
    { id: "web_search", label: "Web Search" },
    { id: "read_url", label: "Read URL" },
    { id: "calculator", label: "Calculator" },
    { id: "upload_artifact", label: "Upload Artifact" },
    ...(devTaskControlsEnabled ? [{ id: "dev_sleep", label: "Dev Sleep" }] : [])
];
```

## Acceptance Criteria

- [ ] `ArtifactMetadata` type added to `services/console/src/types/index.ts`
- [ ] `api.listArtifacts()` method added to `services/console/src/api/client.ts`
- [ ] `api.getArtifactDownloadUrl()` method added to `services/console/src/api/client.ts`
- [ ] `useTaskArtifacts` hook created in `services/console/src/features/task-detail/useArtifacts.ts`
- [ ] `ArtifactsTab` component created in `services/console/src/features/task-detail/ArtifactsTab.tsx`
- [ ] `ArtifactsTab` displays filename, direction, content_type, human-readable size, created_at
- [ ] Download button per artifact triggers `window.open()` to download URL
- [ ] Empty state: `ArtifactsTab` returns `null` when no artifacts exist (no visible section)
- [ ] `ArtifactsTab` integrated into `TaskDetailPage.tsx` — visible after the output section when artifacts exist
- [ ] `upload_artifact` added to `ALLOWED_TOOLS` in `schema.ts`
- [ ] `make test` passes with no regressions (including existing console tests)

## Testing Requirements

- **Unit tests:** Verify `formatFileSize()` produces correct human-readable output (B, KB, MB). Verify `ArtifactsTab` renders artifacts with download buttons. Verify `ArtifactsTab` returns null for empty list. Verify `useTaskArtifacts` hook is called with correct query key.
- **Regression tests:** Run `make test` — all existing console tests (TaskDetailPage, etc.) must still pass. Existing mocks may need updating if `useTaskArtifacts` is called in tests.
- **Browser verification:** After `make start`, navigate to a task detail page. If the task has artifacts (created via integration test or manual tool call), verify the artifacts section appears with download buttons.

## Constraints and Guardrails

- Do not add file upload UI — that is Track 2 (multipart submission).
- Do not add sandbox configuration UI — that is Track 2.
- Do not modify existing components beyond the minimal changes to integrate `ArtifactsTab`.
- Follow existing Console patterns: shadcn/ui components, Tailwind CSS, `font-mono` for data, uppercase tracking-widest for labels.
- Use `react-query` for data fetching — no direct `fetch()` calls in components.
- The `ArtifactsTab` should not refetch on interval — artifacts are immutable after creation.

## Assumptions

- Task 5 has been completed and `GET /v1/tasks/{taskId}/artifacts` is available.
- The API returns artifacts with camelCase field names (Spring Boot Jackson default for records).
- The Console dev server runs at `localhost:5173` with the API proxied at `localhost:8080`.
- Existing `TaskDetailPage` tests mock `useTaskStatus` and other hooks — new `useTaskArtifacts` calls will need to be handled in test mocks (either by adding a mock or by having the hook gracefully handle undefined taskId).
- `lucide-react` icons (`Download`, `FileText`) are already available in the project dependencies.

<!-- AGENT_TASK_END: task-7-console-artifacts-tab.md -->
