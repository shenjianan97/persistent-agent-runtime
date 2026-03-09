<!-- AGENT_TASK_START: task-7-console.md -->

# Task 7: Console (Frontend)

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below, with one exception: you must add CORS configuration to the API Service (see Step 1).

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and API contract:
1. `docs/PROJECT.md`
2. `docs/design/PHASE1_DURABLE_EXECUTION.md` (Sections 3 and 10 for API contract and demo scenario)
3. `services/api-service/src/main/java/com/persistentagent/api/controller/TaskController.java` (exact endpoint signatures)
4. `services/api-service/src/main/java/com/persistentagent/api/model/response/` (all response DTOs)
5. `services/api-service/src/main/java/com/persistentagent/api/model/request/` (submission request shape)
6. `services/api-service/src/main/java/com/persistentagent/api/config/ValidationConstants.java` (allowed models list)

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `docs/implementation_plan/phase-1/progress.md` file.

## Context
The design doc identifies a "Demo Dashboard" as a stretch goal (Section 10). This task promotes it to a full implementation task for Phase 1, providing a polished single-page application that visualizes the durable execution runtime. The console serves as the primary demo artifact — it must look professional and clearly communicate the system's value propositions: task lifecycle management, checkpoint-resume after crash recovery, and per-node cost tracking.

## Tech Stack Selection
- **Framework:** React 19 with TypeScript
- **Build tool:** Vite 6 (fast dev server, modern bundling)
- **Styling:** Tailwind CSS 4 with shadcn/ui components (polished, accessible, consistent look)
- **Data fetching:** TanStack Query v5 (polling, caching, background refetch)
- **Routing:** React Router v7 (lightweight client-side routing)
- **Forms:** React Hook Form + Zod (type-safe validation with minimal boilerplate)
- **Charts:** Recharts 2 (for cost/latency visualizations)
- **Package manager:** npm

## Affected Component
- **Service/Module:** Console (React SPA)
- **File paths:** `services/console/`
- **Change type:** new code
- **Cross-cutting change:** Add a CORS configuration class to the API Service at `services/api-service/src/main/java/com/persistentagent/api/config/CorsConfig.java` to allow `http://localhost:5173` (Vite dev server) and `http://localhost:3000` origins.

## Dependencies
- **Must complete first:** Task 2 (API Service) — the console consumes all REST endpoints
- **Provides output to:** None (demo artifact)
- **Shared interfaces/contracts:** Consumes the exact JSON response shapes from the API Service DTOs

## Local Development Environment
- The API Service runs on `http://localhost:8080` (Spring Boot default).
- The Vite dev server runs on `http://localhost:5173`.
- All API calls use the environment variable `VITE_API_BASE_URL` (default: `http://localhost:8080`). The API client reads this at runtime — no Vite proxy is needed.
- Because the Vite dev server (`localhost:5173`) and the API (`localhost:8080`) are on different origins, the CORS configuration in Step 1c is **required** for browser fetch requests to succeed.
- The same local PostgreSQL container from Task 1 (`persistent-agent-runtime-postgres` on `localhost:55432`) backs the API.

## Implementation Specification

### Step 1: Project Scaffolding and API Service CORS
1a. Scaffold the React project at `services/console/` using Vite with the `react-ts` template. Install dependencies: Tailwind CSS, shadcn/ui (via `npx shadcn@latest init`), TanStack Query, React Router, React Hook Form, Zod, `@hookform/resolvers`, and Recharts.
1b. Create a `.env.example` file (committed) with `VITE_API_BASE_URL=http://localhost:8080` and a `.env` file (gitignored) with the same content. Add `.env` to the project's `.gitignore`. The API client must read `import.meta.env.VITE_API_BASE_URL` as the base URL for all requests. No Vite proxy is needed.
1c. Add a Spring Boot CORS configuration class to the API Service:
```java
// services/api-service/src/main/java/com/persistentagent/api/config/CorsConfig.java
@Configuration
public class CorsConfig implements WebMvcConfigurer {
    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/v1/**")
                .allowedOrigins("http://localhost:5173", "http://localhost:3000")
                .allowedMethods("GET", "POST", "OPTIONS")
                .allowedHeaders("*");
    }
}
```
1d. Set up the project structure. Use a **feature-based** layout — group files by domain feature, not by technical role. Shared primitives live at the top level; feature-specific components, hooks, and types are co-located within their feature folder.
```
services/console/
├── src/
│   ├── api/              # Typed API client (one file per resource: tasks.ts, health.ts)
│   ├── components/ui/    # shadcn/ui primitives (auto-generated by shadcn CLI)
│   ├── features/
│   │   ├── dashboard/    # Overview page component
│   │   ├── submit/       # Task submission form, Zod schema, page component
│   │   ├── task-detail/  # Task detail page, checkpoint timeline, cost chart, status badge
│   │   └── dead-letter/  # Dead letter table, filter, redrive action, page component
│   ├── layout/           # App shell (sidebar, header, health indicator)
│   ├── lib/              # Shared utilities (cost formatting, date formatting, constants)
│   ├── types/            # TypeScript types mirroring API DTOs
│   ├── App.tsx           # Router setup
│   └── main.tsx          # Entry point, QueryClientProvider
├── .env.example          # VITE_API_BASE_URL=http://localhost:8080 (committed)
├── .env                  # Local copy (gitignored)
├── index.html
├── tailwind.config.ts
├── tsconfig.json
├── vite.config.ts
└── package.json
```
**Rationale:** When Phase 2 adds agents, budgets, or memory views, each becomes a new folder under `features/` without touching existing code. Feature-local components stay co-located with their page, reducing cross-directory navigation.

### Step 2: TypeScript Types and API Client
2a. Define TypeScript interfaces that mirror the API response DTOs exactly:
- `TaskStatusResponse` — matches `TaskStatusResponse.java` field names (`task_id`, `agent_id`, `status`, `input`, `output`, `retry_count`, `retry_history`, `checkpoint_count`, `total_cost_microdollars`, `lease_owner`, `last_error_code`, `last_error_message`, `last_worker_id`, `dead_letter_reason`, `dead_lettered_at`, `created_at`, `updated_at`)
- `TaskSubmissionRequest` and `TaskSubmissionResponse`
- `CheckpointResponse` and `CheckpointListResponse`
- `DeadLetterItemResponse` and `DeadLetterListResponse`
- `HealthResponse`
- `TaskCancelResponse`, `RedriveResponse`

2b. Implement a thin API client module (`api/client.ts`) with a shared `fetchApi<T>(path, options)` helper that:
- Prepends `import.meta.env.VITE_API_BASE_URL` to all paths
- Sets `Content-Type: application/json` for POST requests
- Throws a typed `ApiError` (with `status`, `message`) on non-2xx responses
- Returns parsed JSON as `T`

Then expose typed API functions built on `fetchApi`:
- `submitTask(request: TaskSubmissionRequest): Promise<TaskSubmissionResponse>`
- `getTaskStatus(taskId: string): Promise<TaskStatusResponse>`
- `getCheckpoints(taskId: string): Promise<CheckpointListResponse>`
- `cancelTask(taskId: string): Promise<TaskCancelResponse>`
- `listDeadLetterTasks(agentId?: string, limit?: number): Promise<DeadLetterListResponse>`
- `redriveTask(taskId: string): Promise<RedriveResponse>`
- `getHealth(): Promise<HealthResponse>`

2c. Wrap each API call in TanStack Query hooks with appropriate `refetchInterval` polling:
- Task status: `refetchInterval: 2000` while status is `queued` or `running`, `false` on terminal states (`completed`, `cancelled`, `dead_letter`)
- Checkpoints: `refetchInterval: 3000` while task is `running`, `false` otherwise
- Health: `refetchInterval: 10000` (always polling)
- Dead letter list: `refetchInterval: 15000`
- Co-locate these hooks alongside their feature (e.g., `features/task-detail/useTaskStatus.ts`) rather than in a top-level `hooks/` directory

### Step 3: Application Shell and Routing
3a. Build the application shell with a clean sidebar layout:
- **Header:** App name "Persistent Agent Runtime" with a health status indicator (green/red dot based on `/v1/health`)
- **Sidebar navigation:** Dashboard (home), Submit Task, Dead Letter Queue
- **Main content area:** Routed page content

3b. Set up routes:
- `/` — Overview (system health)
- `/tasks/new` — Task submission form
- `/tasks/:taskId` — Task detail view with checkpoint timeline
- `/dead-letter` — Dead letter queue listing

### Step 4: Dashboard Overview Page (`/`)
4a. Display system health from `GET /v1/health`:
- Database status indicator (connected/disconnected)
- Active workers count
- Queued tasks count

4b. This page serves as the landing page and system health overview. Keep it clean and minimal — detailed task views are on their respective pages.

### Step 5: Task Submission Page (`/tasks/new`)
5a. Define a Zod schema (`features/submit/schema.ts`) that mirrors the API validation constraints. Use `@hookform/resolvers/zod` to connect it to React Hook Form. This gives type-safe validation with zero manual error wiring.

5b. Build the form using React Hook Form's `useForm` + shadcn/ui form components. Fields:
- **Agent ID** (required, max 64 chars)
- **Input prompt** (required, textarea, max 100KB) — this is the user's research question or instruction
- **System Prompt** (required, textarea, max 50KB)
- **Model** dropdown — populate with the exact list from `ValidationConstants.java` (read that file to get the current allowed models)
- **Temperature** (0.0–2.0, default 0.7)
- **Allowed Tools** — multi-select checkboxes for `web_search`, `read_url`, `calculator`
- **Max Steps** (1–1000, default 100)
- **Max Retries** (0–10, default 3)
- **Task Timeout** (60–86400 seconds, default 3600)

5c. On successful submission, navigate to `/tasks/:taskId` to watch execution in real time.

5d. Validation errors are displayed inline automatically via React Hook Form's `formState.errors`. Show API error responses (network failures, server-side validation) in a toast/alert.

### Step 6: Task Detail Page (`/tasks/:taskId`) — The Core Demo View
This is the most important page. It must clearly demonstrate the value of durable execution.

6a. **Task Header:**
- Task ID (monospace, copyable)
- Status badge with color coding: `queued` (yellow), `running` (blue/pulsing), `completed` (green), `cancelled` (gray), `dead_letter` (red)
- Agent ID
- Created/updated timestamps (relative, e.g., "2 minutes ago")

6b. **Action Buttons:**
- Cancel button (visible when status is `queued` or `running`)
- Redrive button (visible only when status is `dead_letter`)

6c. **Checkpoint Timeline:**
- Vertical timeline showing each checkpoint as a node
- Each node displays: step number, node name (e.g., "agent", "tools"), worker ID, cost in microdollars (formatted as dollars, e.g., "$0.0023"), timestamp
- Highlight if the worker ID changes mid-execution (demonstrates crash recovery — different worker resumed the task)
- Auto-scroll to the latest checkpoint as new ones arrive
- Show a "live" indicator (pulsing dot) at the bottom of the timeline while the task is running

6d. **Cost Summary Panel:**
- Total cost (formatted from microdollars: `total_cost_microdollars / 1_000_000` → "$X.XXXX")
- Cost per checkpoint bar chart (using Recharts)
- Checkpoint count

6e. **Error Panel** (shown only for dead-lettered tasks):
- Dead letter reason
- Last error code and message
- Retry count at time of dead-letter

6f. **Task Input/Output Panel:**
- Collapsible section showing the original input
- Output section (shown when task is completed) — render as formatted text/JSON

### Step 7: Dead Letter Queue Page (`/dead-letter`)
7a. Table listing dead-lettered tasks with columns: Task ID (linked to detail page), Agent ID, Dead Letter Reason, Error Code, Error Message, Retry Count, Dead Lettered At.
7b. Filter by Agent ID (text input).
7c. Each row has a "Redrive" action button that calls `POST /v1/tasks/{task_id}/redrive`.
7d. On successful redrive, show a success toast and refresh the list.

### Step 8: End-to-End Local Integration Test
After implementation is complete, run a full end-to-end test with all three services running locally. This is a **required** step — the task is not done until this passes.

**8a. Start the stack:**
1. PostgreSQL — ensure the local container is running: `docker ps --filter name=persistent-agent-runtime-postgres`. If not running, start it and apply the schema via `KEEP_DB_CONTAINER=1 ./infrastructure/database/verify_schema.sh`.
2. API Service — start Spring Boot on port 8080 (`./gradlew bootRun` or equivalent from `services/api-service/`).
3. Worker Service — start the Python worker (`python -m worker` or equivalent from `services/worker-service/`).
4. Console — start Vite dev server (`npm run dev` from `services/console/`).

**8b. Test scenario — happy path:**
1. Open the console at `http://localhost:5173`.
2. Verify the health indicator shows green (database connected, workers active).
3. Navigate to `/tasks/new`. Submit a task with agent_id `e2e-test`, a simple input prompt (e.g., "What is 2+2?"), a system prompt, model from the dropdown, and `calculator` as the allowed tool.
4. Confirm auto-navigation to `/tasks/:taskId`. Observe status transition: `queued` → `running` → `completed`.
5. Verify checkpoints appear in the timeline as execution progresses.
6. Verify the cost summary updates and the bar chart renders.
7. Verify the output panel shows the completed result.

**8c. Test scenario — dead letter and redrive:**
1. Submit a task designed to fail (e.g., set `max_steps: 1` with a complex prompt that requires tool use, triggering `max_steps_exceeded`).
2. Observe status transition to `dead_letter` on the task detail page. Verify the error panel shows the dead letter reason.
3. Navigate to `/dead-letter`. Verify the task appears in the table.
4. Click "Redrive". Verify success toast and the task disappears from the dead letter list.
5. Follow the task link — verify it's back in `queued` status.

**8d. Test scenario — cancel:**
1. Submit a task with a high `max_steps` value so it runs long enough to cancel.
2. On the task detail page, click "Cancel" while status is `running`.
3. Verify status transitions to `cancelled`.

**8e. Test scenario — API unavailable:**
1. Stop the API Service.
2. Verify the health indicator turns red and the error banner appears ("API unavailable").
3. Restart the API Service. Verify the health indicator recovers to green automatically via polling.

### Step 9: Polish and Demo Readiness
9a. **Dark mode:** Default to a dark theme (common for developer tools / monitoring dashboards). Use Tailwind's dark mode classes.
9b. **Loading states:** Skeleton loaders for all data-dependent components.
9c. **Empty states:** Meaningful empty states for each page (e.g., "No tasks submitted yet. Submit your first task.").
9d. **Responsive design:** Must look good at 1280px+ (demo will likely be on a laptop/projector). Tablet/mobile is not required.
9e. **Error boundaries:** Graceful error display if the API is unreachable (banner: "API unavailable — is the API service running on port 8080?").
9f. **Favicon and title:** Set page title to "Persistent Agent Runtime — Console".

## Acceptance Criteria
The implementation is complete when:
- [ ] `npm run dev` starts the console and it connects to the local API service
- [ ] Task submission form validates inputs and creates tasks via the API
- [ ] Task detail page shows live-updating checkpoint timeline with cost tracking
- [ ] Status badges correctly reflect all task lifecycle states (queued, running, completed, cancelled, dead_letter)
- [ ] Crash recovery is visually evident: when a worker change occurs mid-execution, the timeline highlights the worker handoff
- [ ] Dead letter queue page lists failed tasks and supports redrive
- [ ] Health indicator in the header reflects API/database connectivity
- [ ] Dark theme is applied and the UI looks polished and professional
- [ ] CORS is configured in the API Service for local development
- [ ] End-to-end local test passes: task submission → live checkpoint timeline → completion, dead letter → redrive, cancel, and API-down recovery all verified with PostgreSQL + API Service + Worker Service + Console running together

## Testing Requirements
- **Type checking:** `tsc --noEmit` passes with zero errors
- **Lint:** ESLint with the recommended React/TypeScript rules passes
- **Component tests:** Use Vitest + React Testing Library for key interactive components:
  - Task submission form validation (required fields, range limits)
  - Status badge renders correct color for each status
  - Checkpoint timeline renders checkpoint data correctly
  - Dead letter table renders items and redrive button triggers API call
- **API client tests:** Mock fetch responses and verify typed parsing
- **Build:** `npm run build` produces a production bundle without errors

## Constraints and Guardrails
- Do not modify any API Service logic beyond adding the CORS configuration class
- Do not add authentication/authorization — Phase 1 has no auth
- Do not implement WebSocket or SSE — use HTTP polling via TanStack Query (matches the Phase 1 design which uses database polling, not push)
- Do not implement a task list/search page — individual tasks are accessed via their ID from submission or the dead letter queue. A full task search API is not part of the Phase 1 contract
- Cost display must use the `total_cost_microdollars` field divided by 1,000,000 — do not fabricate cost data
- All API interactions must go through the typed API client — no raw fetch calls in components

## Assumptions / Open Questions for This Task
- ASSUMPTION: The API Service runs on `http://localhost:8080` with default Spring Boot configuration.
- ASSUMPTION: Models listed in the submission form match those validated in `ValidationConstants.java`. The implementing agent MUST read that file and use the exact model list.
- RESOLVED: The design doc Section 10 describes the demo dashboard as a "stretch goal" — this task promotes it to a required deliverable for Phase 1 demo readiness.

<!-- AGENT_TASK_END: task-7-console.md -->
