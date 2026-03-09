import { z } from 'zod';

export const submitTaskSchema = z.object({
    agent_id: z.string().min(1, 'Agent ID is required').max(64),
    input: z.string().min(1, 'Input prompt is required').max(102400),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).optional().default(0.7),
    allowed_tools: z.array(z.string()).optional(),
    max_steps: z.number().int().min(1).max(1000).optional().default(100),
    max_retries: z.number().int().min(0).max(10).optional().default(3),
    task_timeout_seconds: z.number().int().min(60).max(86400).optional().default(3600),
});

export type SubmitTaskFormValues = z.infer<typeof submitTaskSchema>;

// Models from ValidationConstants.java
export const SUPPORTED_MODELS = [
    "claude-sonnet-4-6",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-20250514",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-haiku-4-20250514-v1:0"
];

// Tools from ValidationConstants.java
export const ALLOWED_TOOLS = [
    { id: "web_search", label: "Web Search" },
    { id: "read_url", label: "Read URL" },
    { id: "calculator", label: "Calculator" }
];
