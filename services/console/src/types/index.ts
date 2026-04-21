export type TaskStatus = 'queued' | 'running' | 'completed' | 'cancelled' | 'dead_letter' | 'waiting_for_approval' | 'waiting_for_input' | 'paused';

export type TaskEventType = 'task_submitted' | 'task_claimed' | 'task_retry_scheduled' |
    'task_reclaimed_after_lease_expiry' | 'task_dead_lettered' | 'task_redriven' |
    'task_completed' | 'task_paused' | 'task_resumed' | 'task_approval_requested' |
    'task_approved' | 'task_rejected' | 'task_input_requested' | 'task_input_received' | 'task_cancelled' |
    'task_follow_up' | 'task_compaction_fired';

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
    /** Attached memory ids in position order. Empty for tasks without attachments. */
    attached_memory_ids?: string[];
    /** Live preview (memory_id + title) for attachments that still resolve within scope. */
    attached_memories_preview?: AttachedMemoryPreview[];
    /** Memory write mode for this task: 'always' | 'agent_decides' | 'skip'. */
    memory_mode: string;
}

export interface AttachedMemoryPreview {
    memory_id: string;
    title: string;
}

export interface TaskSubmissionRequest {
    agent_id: string;
    input: string;
    max_steps?: number;
    max_retries?: number;
    task_timeout_seconds?: number;
    langfuse_endpoint_id?: string;
    /** Optional list of memory entry ids to attach (ordered). Omitted when empty. */
    attached_memory_ids?: string[];
    /** Per-task memory-write mode. Optional; server default is 'always'. */
    memory_mode?: 'always' | 'agent_decides' | 'skip';
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

export interface SandboxConfig {
    enabled: boolean;
    template?: string;
    vcpu?: number;
    memory_mb?: number;
    timeout_seconds?: number;
}

export interface MemoryConfig {
    enabled?: boolean;
    summarizer_model?: string;
    max_entries?: number;
}

export interface ContextManagementConfig {
    summarizer_model?: string;
    exclude_tools?: string[];
    pre_tier3_memory_flush?: boolean;
    /**
     * Track 7 Follow-up (Task 4) — Tier 0 ingestion offload kill switch.
     * Default `true` (applied server-side when absent). Not rendered in v1;
     * field exists to preserve round-trip stability of the `context_management`
     * sub-object when the worker writes it back into `agent_config`.
     */
    offload_tool_results?: boolean;
}

export interface AgentConfig {
    system_prompt: string;
    provider: string;
    model: string;
    temperature: number;
    allowed_tools: string[];
    tool_servers?: string[];
    sandbox?: SandboxConfig;
    memory?: MemoryConfig;
    context_management?: ContextManagementConfig;
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
    agent_config: Omit<AgentConfig, 'temperature' | 'allowed_tools' | 'tool_servers' | 'sandbox'> & {
        temperature?: number;
        allowed_tools?: string[];
        tool_servers?: string[];
        sandbox?: SandboxConfig;
    };
    max_concurrent_tasks?: number;
    budget_max_per_task?: number;
    budget_max_per_hour?: number;
}

export interface AgentUpdateRequest {
    display_name: string;
    agent_config: Omit<AgentConfig, 'temperature' | 'allowed_tools' | 'tool_servers' | 'sandbox'> & {
        temperature?: number;
        allowed_tools?: string[];
        tool_servers?: string[];
        sandbox?: SandboxConfig;
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

// Memory types (Phase 2 Track 5)
export type MemoryOutcome = 'succeeded' | 'failed';

export interface MemoryStorageStats {
    entry_count: number;
    approx_bytes: number;
}

export interface MemoryEntrySummary {
    memory_id: string;
    title: string;
    outcome: MemoryOutcome;
    task_id: string;
    created_at: string;
    summary_preview?: string;
    score?: number;
}

export interface MemoryListResponse {
    items: MemoryEntrySummary[];
    next_cursor?: string;
    agent_storage_stats?: MemoryStorageStats;
}

export interface MemorySearchResponse {
    results: MemoryEntrySummary[];
    ranking_used: 'hybrid' | 'text' | 'vector';
}

export interface MemoryEntryResponse {
    memory_id: string;
    agent_id: string;
    task_id: string;
    title: string;
    summary: string;
    observations: string[];
    outcome: MemoryOutcome;
    tags: string[];
    summarizer_model_id?: string;
    version: number;
    created_at: string;
    updated_at: string;
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

// ──────────────────────────────────────────────────────────────────
// User-facing Conversation log (Phase 2 Track 7 Task 13)
// ──────────────────────────────────────────────────────────────────

export type ConversationEntryKind =
    | 'user_turn'
    | 'agent_turn'
    | 'tool_call'
    | 'tool_result'
    | 'compaction_boundary'
    | 'memory_flush'
    | 'hitl_pause'
    | 'hitl_resume'
    | 'system_note'
    // Phase 2 Track 7 Follow-up Task 5 — one entry per ingestion-offload
    // pass that moved ≥1 tool result / arg to S3. Compact inline notice
    // (smaller than a Tier 3 boundary, not a full divider).
    | 'offload_emitted';

/**
 * Metadata attached to a conversation entry. Shape is intentionally permissive:
 * the pane renders only fields it recognises for a given `kind`, and falls back
 * to a debug-fold banner when a field is missing or `content_version > 1`.
 */
export interface ConversationEntryMetadata {
    capped?: boolean;
    orig_bytes?: number;
    turns_summarized?: number;
    summarizer_model?: string;
    summary_bytes?: number;
    cost_microdollars?: number;
    tier3_firing_index?: number;
    first_turn_index?: number;
    last_turn_index?: number;
    // offload_emitted — the content is the authoritative payload; metadata
    // stays empty in v1. Declared here so render sites can opt into a
    // roll-up summary without a type cast.
    offload_count?: number;
    offload_total_bytes?: number;
    [key: string]: unknown;
}

export interface ConversationEntry {
    /** Stable monotonically-increasing sequence within a task. */
    sequence: number;
    /** One of the 9 public kinds; clients must handle unknown values gracefully. */
    kind: ConversationEntryKind | string;
    /** Schema version; clients fall back to a debug-fold banner when > 1. */
    content_version: number;
    /** Free-form content object keyed by `kind`; see the task spec §Content schema. */
    content: Record<string, unknown>;
    /** Render hints (capping info, compaction provenance, etc.). */
    metadata?: ConversationEntryMetadata;
    /** ISO timestamp the entry was produced. */
    created_at: string;
}

export interface ConversationListResponse {
    entries: ConversationEntry[];
    /** Present when more entries exist past the requested window. */
    next_sequence?: number;
}

// ─── Phase 2 Track 7 Follow-up Task 8 — Unified Activity projection ───
//
// Discriminated-union shape returned by `GET /v1/tasks/{taskId}/activity`.
// Consumers switch on `kind` and ignore fields they don't recognise.

export type ActivityEventKind =
    | 'turn.user'
    | 'turn.assistant'
    | 'turn.tool'
    | 'marker.compaction_fired'
    | 'marker.memory_flush'
    | 'marker.offload_emitted'
    | 'marker.system_note'
    | 'marker.lifecycle'
    | 'marker.hitl.paused'
    | 'marker.hitl.approval_requested'
    | 'marker.hitl.input_requested'
    | 'marker.hitl.approved'
    | 'marker.hitl.rejected'
    | 'marker.hitl.input_received'
    | 'marker.hitl.resumed';

export interface ActivityToolCall {
    id?: string;
    name?: string;
    args?: unknown;
}

export interface ActivityEvent {
    kind: ActivityEventKind | string;
    timestamp: string | null;
    role?: string | null;
    content?: string | null;
    tool_name?: string | null;
    tool_call_id?: string | null;
    tool_calls?: ActivityToolCall[] | null;
    is_error?: boolean | null;
    event_type?: string | null;
    status_before?: string | null;
    status_after?: string | null;
    summary_text?: string | null;
    details?: Record<string, unknown> | null;
}

export interface ActivityListResponse {
    events: ActivityEvent[];
    next_cursor: string | null;
}
