# Frontend Console E2E Scenarios (Playwright / Cypress)

Since the frontend is a React 19 SPA running in the browser, testing requires a browser automation framework (e.g., Playwright or Cypress) to interact with the DOM, simulate user clicks, and verify visual state changes based on backend polling.

**Prerequisites:** 
- API Service running (`localhost:8080`)
- Worker Service running (with mock LLM as described in Section 2)
- Vite Dev Server or Production Build running (`localhost:5173`)

### 1. E2E-UI-1: Happy Path Task Submission & Live Timeline
**What it validates:** The user can fill out the task submission form, submit it successfully, navigate to the detail view, and watch the execution timeline populate live as the worker processes the task.

**Test Steps:**
1. Navigate to `http://localhost:5173/tasks/new`.
2. Fill "Agent ID" with `e2e-test-agent`.
3. Select "claude-sonnet-4-6" from the Model dropdown.
4. Check the `calculator` tool.
5. Enter "What is 10 + 10?" into the Input Directive textarea.
6. Click "DISPATCH TASK".
7. **Assertions:**
   - Toast notification appears: "Task [...uuid...] submitted".
   - URL changes to `/tasks/[...uuid...]`.
   - The status badge reads `QUEUED` then changes to `RUNNING` (Neon Cyan).
   - "Execution Timeline" panel appears and begins rendering checkpoints.
   - Wait for status badge to change to `COMPLETED` (Acid Green).
   - Verify the `Cost Tracker` chart renders a bar.
   - Verify the `Output` panel displays "20".

### 2. E2E-UI-2: Handling Execution Failure (Dead Letter)
**What it validates:** The UI gracefully handles an execution failure without crashing, correctly displaying the `DEAD_LETTER` state and the error reason provided by the API.

**Test Steps:**
1. Configure the worker's mock LLM to throw a non-retryable error (e.g., "400 Bad Request").
2. Navigate to `http://localhost:5173/tasks/new` and submit a generic task.
3. Arrive at the Task Detail view.
4. **Assertions:**
   - Status badge transitions to `HEADS UP: DEAD LETTER` (Alert Red).
   - The `Execution Failure` panel appears.
   - The error text contains "400 Bad Request".
   - The Timeline shows "Execution Halted".

### 3. E2E-UI-3: Dead Letter Queue Filter & Redrive
**What it validates:** The user can view failed tasks in the DLQ table and successfully trigger a redrive action.

**Test Steps:**
1. Ensure at least one task is in the `dead_letter` state (from 13.2).
2. Navigate to `http://localhost:5173/dead-letter`.
3. **Assertions:**
   - The table renders the dead-lettered task.
4. Click the "REDRIVE TASK" button on that row.
5. **Assertions:**
   - Toast notification indicates successful redrive.
   - The task disappears from the DLQ table (since its status is now `queued`).
   - (Optional) Navigate back to the task's detail page and verify it resumes execution.

### 4. E2E-UI-4: Form Validation & Error States
**What it validates:** Zod schema validation prevents malformed requests from hitting the API.

**Test Steps:**
1. Navigate to `http://localhost:5173/tasks/new`.
2. Leave all fields blank/default except clearing the "Max Steps" to be empty or `0`.
3. Click "DISPATCH TASK".
4. **Assertions:**
   - Form submission is blocked.
   - Validation text "Number must be greater than or equal to 1" appears under Max Steps.
   - Validation text "String must contain at least 1 character(s)" appears under Agent ID.
