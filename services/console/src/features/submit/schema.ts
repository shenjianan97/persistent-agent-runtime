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
export const ALLOWED_TOOLS = [
    { id: "web_search", label: "Web Search" },
    { id: "read_url", label: "Read URL" },
    { id: "calculator", label: "Calculator" },
    ...(devTaskControlsEnabled ? [{ id: "dev_sleep", label: "Dev Sleep" }] : [])
];

// Runtime tool ID for human-in-the-loop — presented as a separate toggle
export const HUMAN_INPUT_TOOL_ID = "request_human_input";
