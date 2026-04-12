export type TaskStatus = 'queued' | 'running' | 'completed' | 'cancelled' | 'dead_letter' | 'waiting_for_approval' | 'waiting_for_input' | 'paused';

export type TaskEventType = 'task_submitted' | 'task_claimed' | 'task_retry_scheduled' |
    'task_reclaimed_after_lease_expiry' | 'task_dead_lettered' | 'task_redriven' |
    'task_completed' | 'task_paused' | 'task_resumed' | 'task_approval_requested' |
    'task_approved' | 'task_rejected' | 'task_input_requested' | 'task_input_received' | 'task_cancelled' |
    'task_follow_up';

export interface TaskEventResponse {
    event_id: string;
    task_id: string;
    agent_id: string;
    event_type: TaskEventType;
    status_before?: string;
    status_after?: string;
    worker_id?: string;
    error_code?: string;
    error_message?: string;
    details?: Record<string, unknown>;
    created_at: string;
}

export interface TaskEventListResponse {
    events: TaskEventResponse[];
}

export interface TaskStatusResponse {
    task_id: string;
    agent_id: string;
    agent_display_name: string | null;
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
    langfuse_endpoint_id?: string;
    pending_input_prompt?: string;
    pending_approval_action?: Record<string, unknown>;
    human_input_timeout_at?: string;
    pause_reason?: 'budget_per_task' | 'budget_per_hour' | null;
    pause_details?: {
        budget_max_per_task?: number;
        budget_max_per_hour?: number;
        observed_task_cost_microdollars?: number;
        observed_hour_cost_microdollars?: number;
        recovery_mode?: 'manual_resume_after_budget_increase' | 'automatic_after_window_clears';
    } | null;
    resume_eligible_at?: string | null;
    created_at: string;
    updated_at: string;
}

export interface TaskSubmissionRequest {
    agent_id: string;
    input: string;
    max_steps?: number;
    max_retries?: number;
    task_timeout_seconds?: number;
    langfuse_endpoint_id?: string;
}

export interface TaskSubmissionResponse {
    task_id: string;
    status: string;
    agent_display_name: string | null;
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

export interface TaskObservabilityItemResponse {
    item_id: string;
    parent_item_id?: string | null;
    kind: 'checkpoint_persisted' | 'resumed_after_retry' | 'completed' | 'dead_lettered';
    title: string;
    summary: string;
    step_number?: number | null;
    node_name?: string | null;
    tool_name?: string | null;
    model_name?: string | null;
    cost_microdollars: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    duration_ms?: number | null;
    input?: unknown;
    output?: unknown;
    started_at?: string | null;
    ended_at?: string | null;
}

export interface TaskObservabilityResponse {
    enabled: boolean;
    task_id: string;
    agent_id: string;
    status: string;
    total_cost_microdollars: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    duration_ms?: number | null;
    items: TaskObservabilityItemResponse[];
}

export interface DeadLetterItemResponse {
    task_id: string;
    agent_id: string;
    agent_display_name: string | null;
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
    agent_display_name: string | null;
    status: TaskStatus;
    retry_count: number;
    checkpoint_count: number;
    total_cost_microdollars: number;
    pause_reason?: 'budget_per_task' | 'budget_per_hour' | null;
    resume_eligible_at?: string | null;
    created_at: string;
    updated_at: string;
}

export interface TaskListResponse {
    items: TaskSummaryResponse[];
    total: number;
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

export interface LangfuseEndpoint {
    endpoint_id: string;
    tenant_id: string;
    name: string;
    host: string;
    created_at: string;
    updated_at: string;
}

export interface LangfuseEndpointRequest {
    name: string;
    host: string;
    public_key: string;
    secret_key: string;
}

export interface LangfuseEndpointTestResponse {
    reachable: boolean;
    message: string;
}

export interface AgentSummaryResponse {
    agent_id: string;
    display_name: string;
    provider: string;
    model: string;
    status: 'active' | 'disabled';
    max_concurrent_tasks: number;
    budget_max_per_task: number;
    budget_max_per_hour: number;
    created_at: string;
    updated_at: string;
}

export interface AgentConfig {
    system_prompt: string;
    provider: string;
    model: string;
    temperature: number;
    allowed_tools: string[];
    tool_servers?: string[];
}

export interface AgentResponse {
    agent_id: string;
    display_name: string;
    agent_config: AgentConfig;
    status: 'active' | 'disabled';
    max_concurrent_tasks: number;
    budget_max_per_task: number;
    budget_max_per_hour: number;
    created_at: string;
    updated_at: string;
}

export interface AgentCreateRequest {
    display_name: string;
    agent_config: Omit<AgentConfig, 'temperature' | 'allowed_tools' | 'tool_servers'> & {
        temperature?: number;
        allowed_tools?: string[];
        tool_servers?: string[];
    };
    max_concurrent_tasks?: number;
    budget_max_per_task?: number;
    budget_max_per_hour?: number;
}

export interface AgentUpdateRequest {
    display_name: string;
    agent_config: Omit<AgentConfig, 'temperature' | 'allowed_tools' | 'tool_servers'> & {
        temperature?: number;
        allowed_tools?: string[];
        tool_servers?: string[];
    };
    status: 'active' | 'disabled';
    max_concurrent_tasks?: number;
    budget_max_per_task?: number;
    budget_max_per_hour?: number;
}

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
