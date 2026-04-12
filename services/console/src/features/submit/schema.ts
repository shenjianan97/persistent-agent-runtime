import { z } from 'zod';

const devTaskControlsEnabled = import.meta.env.VITE_DEV_TASK_CONTROLS_ENABLED === 'true';

export const submitTaskSchema = z.object({
    agent_id: z.string().min(1, 'Agent is required'),
    input: z.string().min(1, 'Input prompt is required').max(102400),
    max_steps: z.number().int().min(1).max(1000).optional().default(100),
    max_retries: z.number().int().min(0).max(10).optional().default(3),
    task_timeout_seconds: z.number().int().min(devTaskControlsEnabled ? 1 : 60).max(86400).optional().default(3600),
    langfuse_endpoint_id: z.string().optional(),
});

export type SubmitTaskFormValues = z.infer<typeof submitTaskSchema>;

// Capability tools — do external work and return results
// Only user-selectable tools appear here. Auto-managed tools (upload_artifact, sandbox_*)
// and removed tools (calculator) are excluded from this list.
export const ALLOWED_TOOLS = [
    { id: "web_search", label: "Web Search" },
    { id: "read_url", label: "Read URL" },
    ...(devTaskControlsEnabled ? [{ id: "dev_sleep", label: "Dev Sleep" }] : [])
];

// Full label map for display purposes — includes auto-managed and legacy tools
// so read-only views can show human-readable labels for any stored tool ID.
export const ALL_TOOL_LABELS: Record<string, string> = {
    web_search: "Web Search",
    read_url: "Read URL",
    calculator: "Calculator",
    sandbox_exec: "Sandbox Exec",
    sandbox_read_file: "Sandbox Read File",
    sandbox_write_file: "Sandbox Write File",
    sandbox_download: "Sandbox Download",
    upload_artifact: "Upload Artifact",
    request_human_input: "Human Input",
    dev_sleep: "Dev Sleep",
};

// Runtime tool ID for human-in-the-loop — presented as a separate toggle
export const HUMAN_INPUT_TOOL_ID = "request_human_input";
