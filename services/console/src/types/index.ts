export type TaskStatus = 'queued' | 'running' | 'completed' | 'cancelled' | 'dead_letter';

export interface TaskStatusResponse {
    task_id: string;
    agent_id: string;
    status: TaskStatus;
    input: string;
    output?: unknown;
    retry_count: number;
    retry_history: string[];
    checkpoint_count: number;
    total_cost_microdollars: number;
    lease_owner?: string;
    last_error_code?: string;
    last_error_message?: string;
    last_worker_id?: string;
    dead_letter_reason?: string;
    dead_lettered_at?: string;
    created_at: string;
    updated_at: string;
}

export interface TaskSubmissionRequest {
    agent_id: string;
    input: string;
    system_prompt: string;
    provider: string;
    model: string;
    temperature?: number;
    allowed_tools?: string[];
    max_steps?: number;
    max_retries?: number;
    task_timeout_seconds?: number;
}

export interface TaskSubmissionResponse {
    task_id: string;
    status: string;
}

export interface CheckpointResponse {
    checkpoint_id: string;
    task_id: string;
    step_number: number;
    node_name: string;
    worker_id: string;
    cost_microdollars: number;
    event?: CheckpointEvent;
    state_snapshot?: any;
    created_at: string;
}

export interface CheckpointEventUsage {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
}

export interface CheckpointEvent {
    type: 'system' | 'checkpoint' | 'input' | 'tool_call' | 'tool_result' | 'output';
    title: string;
    summary: string;
    content?: unknown;
    tool_name?: string | null;
    tool_args?: unknown;
    tool_result?: unknown;
    usage?: CheckpointEventUsage | null;
}

export interface CheckpointListResponse {
    checkpoints: CheckpointResponse[];
    total_cost_microdollars: number;
}

export interface DeadLetterItemResponse {
    task_id: string;
    agent_id: string;
    dead_letter_reason: string;
    last_error_code?: string;
    last_error_message?: string;
    retry_count: number;
    dead_lettered_at: string;
}

export interface DeadLetterListResponse {
    items: DeadLetterItemResponse[];
    total: number;
}

export interface TaskSummaryResponse {
    task_id: string;
    agent_id: string;
    status: TaskStatus;
    retry_count: number;
    checkpoint_count: number;
    total_cost_microdollars: number;
    created_at: string;
    updated_at: string;
}

export interface TaskListResponse {
    items: TaskSummaryResponse[];
    total: number;
}

export interface HealthResponse {
    status: string;
    database: string;
    active_workers: number;
    queued_tasks: number;
}

export interface TaskCancelResponse {
    task_id: string;
    status: string;
    message: string;
}

export interface RedriveResponse {
    task_id: string;
    status: string;
    message: string;
}

export interface ModelResponse {
    provider: string;
    model_id: string;
    display_name: string;
}
