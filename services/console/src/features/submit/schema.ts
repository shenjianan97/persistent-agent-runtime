import { z } from 'zod';

const devTaskControlsEnabled = import.meta.env.VITE_DEV_TASK_CONTROLS_ENABLED === 'true';

export const submitTaskSchema = z.object({
    agent_id: z.string().min(1, 'Agent ID is required').max(64),
    input: z.string().min(1, 'Input prompt is required').max(102400),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).optional().default(0.7),
    allowed_tools: z.array(z.string()).optional(),
    max_steps: z.number().int().min(1).max(1000).optional().default(100),
    max_retries: z.number().int().min(0).max(10).optional().default(3),
    task_timeout_seconds: z.number().int().min(devTaskControlsEnabled ? 1 : 60).max(86400).optional().default(3600),
});

export type SubmitTaskFormValues = z.infer<typeof submitTaskSchema>;

// Tools from ValidationConstants.java
export const ALLOWED_TOOLS = [
    { id: "web_search", label: "Web Search" },
    { id: "read_url", label: "Read URL" },
    { id: "calculator", label: "Calculator" },
    ...(devTaskControlsEnabled ? [{ id: "dev_sleep", label: "Dev Sleep" }] : [])
];
