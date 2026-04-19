import type { ArtifactMetadata } from '@/types';
import {
    TaskStatusResponse,
    TaskSubmissionRequest,
    TaskSubmissionResponse,
    TaskListResponse,
    CheckpointListResponse,
    TaskObservabilityResponse,
    DeadLetterListResponse,
    TaskCancelResponse,
    RedriveResponse,
    ModelResponse,
    LangfuseEndpoint,
    LangfuseEndpointRequest,
    LangfuseEndpointTestResponse,
    AgentSummaryResponse,
    AgentResponse,
    AgentCreateRequest,
    AgentUpdateRequest,
    TaskEventListResponse,
    ToolServerSummaryResponse,
    ToolServerResponse,
    ToolServerCreateRequest,
    ToolServerUpdateRequest,
    ToolDiscoverResponse,
    MemoryListResponse,
    MemorySearchResponse,
    MemoryEntryResponse,
} from '@/types';

export class ApiError extends Error {
    constructor(public status: number, message: string) {
        super(message);
        this.name = 'ApiError';
    }
}

function getApiBaseUrl(): string {
    const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
    return configuredBaseUrl ? configuredBaseUrl.replace(/\/+$/, '') : '';
}

async function fetchApi<T>(path: string, options?: RequestInit): Promise<T> {
    const baseUrl = getApiBaseUrl();
    const url = baseUrl ? `${baseUrl}${path}` : path;

    const headers = new Headers(options?.headers);
    if (!headers.has('Content-Type') && options?.method !== 'GET' && options?.body && !(options.body instanceof FormData)) {
        headers.set('Content-Type', 'application/json');
    }

    const response = await fetch(url, { ...options, headers });

    if (!response.ok) {
        let message = response.statusText;
        try {
            const errorData = await response.json();
            if (errorData.message) {
                message = errorData.message;
            } else if (errorData.error) {
                message = errorData.error;
            }
        } catch {
            // Ignored
        }
        throw new ApiError(response.status, message);
    }

    const text = await response.text();
    return text ? JSON.parse(text) as T : {} as T;
}

/**
 * Serialize the submit-task body to JSON. Extracted so the JSON and multipart
 * code paths share the same shape, including the Track-5 `attached_memory_ids`
 * and `memory_mode` fields. Empty attachment list is omitted from the payload
 * so the wire shape stays compact; `memory_mode` is always included (defaults
 * to `'always'` when unset).
 */
function buildSubmitTaskBody(request: TaskSubmissionRequest): string {
    const body: Record<string, unknown> = {
        agent_id: request.agent_id,
        input: request.input,
        max_steps: request.max_steps,
        max_retries: request.max_retries,
        task_timeout_seconds: request.task_timeout_seconds,
        langfuse_endpoint_id: request.langfuse_endpoint_id,
        memory_mode: request.memory_mode ?? 'always',
    };
    if (request.attached_memory_ids && request.attached_memory_ids.length > 0) {
        body.attached_memory_ids = request.attached_memory_ids;
    }
    return JSON.stringify(body);
}

export const api = {
    submitTask: (request: TaskSubmissionRequest) =>
        fetchApi<TaskSubmissionResponse>('/v1/tasks', {
            method: 'POST',
            body: buildSubmitTaskBody(request),
        }),

    submitTaskMultipart: (request: TaskSubmissionRequest, files: File[]) => {
        const formData = new FormData();
        formData.append('task_request', buildSubmitTaskBody(request));
        for (const file of files) {
            formData.append('files', file);
        }
        // Pass FormData as body; fetchApi will NOT auto-set Content-Type for FormData,
        // allowing the browser to set the correct multipart boundary.
        return fetchApi<TaskSubmissionResponse>('/v1/tasks', {
            method: 'POST',
            body: formData,
        });
    },

    listTasks: (status?: string, agentId?: string, limit?: number, pauseReason?: string) => {
        const params = new URLSearchParams();
        if (status) params.append('status', status);
        if (agentId) params.append('agent_id', agentId);
        if (limit) params.append('limit', limit.toString());
        if (pauseReason) params.append('pause_reason', pauseReason);
        const query = params.toString();
        return fetchApi<TaskListResponse>(`/v1/tasks${query ? `?${query}` : ''}`);
    },

    getTaskStatus: (taskId: string) =>
        fetchApi<TaskStatusResponse>(`/v1/tasks/${taskId}`),

    getCheckpoints: (taskId: string) =>
        fetchApi<CheckpointListResponse>(`/v1/tasks/${taskId}/checkpoints`),

    getTaskObservability: (taskId: string) =>
        fetchApi<TaskObservabilityResponse>(`/v1/tasks/${taskId}/observability`),

    cancelTask: (taskId: string) =>
        fetchApi<TaskCancelResponse>(`/v1/tasks/${taskId}/cancel`, {
            method: 'POST',
        }),

    listDeadLetterTasks: (agentId?: string, limit?: number) => {
        const params = new URLSearchParams();
        if (agentId) params.append('agent_id', agentId);
        if (limit) params.append('limit', limit.toString());
        const query = params.toString();
        return fetchApi<DeadLetterListResponse>(`/v1/tasks/dead-letter${query ? `?${query}` : ''}`);
    },

    redriveTask: (taskId: string) =>
        fetchApi<RedriveResponse>(`/v1/tasks/${taskId}/redrive`, {
            method: 'POST',
        }),

    resumeTask: (taskId: string) =>
        fetchApi<RedriveResponse>(`/v1/tasks/${taskId}/resume`, {
            method: 'POST',
        }),

    getModels: () =>
        fetchApi<ModelResponse[]>('/v1/models'),

    // Langfuse Endpoints
    createLangfuseEndpoint: (request: LangfuseEndpointRequest) =>
        fetchApi<LangfuseEndpoint>('/v1/langfuse-endpoints', {
            method: 'POST',
            body: JSON.stringify(request),
        }),

    listLangfuseEndpoints: () =>
        fetchApi<LangfuseEndpoint[]>('/v1/langfuse-endpoints'),

    getLangfuseEndpoint: (endpointId: string) =>
        fetchApi<LangfuseEndpoint>(`/v1/langfuse-endpoints/${endpointId}`),

    updateLangfuseEndpoint: (endpointId: string, request: LangfuseEndpointRequest) =>
        fetchApi<LangfuseEndpoint>(`/v1/langfuse-endpoints/${endpointId}`, {
            method: 'PUT',
            body: JSON.stringify(request),
        }),

    deleteLangfuseEndpoint: (endpointId: string) =>
        fetchApi<void>(`/v1/langfuse-endpoints/${endpointId}`, {
            method: 'DELETE',
        }),

    testLangfuseEndpoint: (endpointId: string) =>
        fetchApi<LangfuseEndpointTestResponse>(`/v1/langfuse-endpoints/${endpointId}/test`, {
            method: 'POST',
        }),

    // Agents
    createAgent: (request: AgentCreateRequest) =>
        fetchApi<AgentResponse>('/v1/agents', {
            method: 'POST',
            body: JSON.stringify(request),
        }),

    listAgents: (status?: string, limit?: number) => {
        const params = new URLSearchParams();
        if (status) params.set('status', status);
        if (limit) params.set('limit', limit.toString());
        const query = params.toString();
        return fetchApi<AgentSummaryResponse[]>(`/v1/agents${query ? '?' + query : ''}`);
    },

    getAgent: (agentId: string) =>
        fetchApi<AgentResponse>(`/v1/agents/${encodeURIComponent(agentId)}`),

    updateAgent: (agentId: string, request: AgentUpdateRequest) =>
        fetchApi<AgentResponse>(`/v1/agents/${encodeURIComponent(agentId)}`, {
            method: 'PUT',
            body: JSON.stringify(request),
        }),

    // HITL Actions
    approveTask: (taskId: string) =>
        fetchApi<TaskStatusResponse>(`/v1/tasks/${taskId}/approve`, {
            method: 'POST',
        }),

    rejectTask: (taskId: string, reason: string) =>
        fetchApi<TaskStatusResponse>(`/v1/tasks/${taskId}/reject`, {
            method: 'POST',
            body: JSON.stringify({ reason }),
        }),

    respondToTask: (taskId: string, message: string) =>
        fetchApi<TaskStatusResponse>(`/v1/tasks/${taskId}/respond`, {
            method: 'POST',
            body: JSON.stringify({ message }),
        }),

    followUpTask: (taskId: string, input: string) =>
        fetchApi<RedriveResponse>(`/v1/tasks/${encodeURIComponent(taskId)}/follow-up`, {
            method: 'POST',
            body: JSON.stringify({ message: input }),
        }),

    // Task Events
    getTaskEvents: (taskId: string, limit = 100) =>
        fetchApi<TaskEventListResponse>(`/v1/tasks/${taskId}/events?limit=${limit}`),

    // Tool Servers
    createToolServer: (request: ToolServerCreateRequest) =>
        fetchApi<ToolServerResponse>('/v1/tool-servers', {
            method: 'POST',
            body: JSON.stringify(request),
        }),

    listToolServers: (status?: string) => {
        const params = new URLSearchParams();
        if (status) params.set('status', status);
        const query = params.toString();
        return fetchApi<ToolServerSummaryResponse[]>(`/v1/tool-servers${query ? '?' + query : ''}`);
    },

    getToolServer: (serverId: string) =>
        fetchApi<ToolServerResponse>(`/v1/tool-servers/${encodeURIComponent(serverId)}`),

    updateToolServer: (serverId: string, request: ToolServerUpdateRequest) =>
        fetchApi<ToolServerResponse>(`/v1/tool-servers/${encodeURIComponent(serverId)}`, {
            method: 'PUT',
            body: JSON.stringify(request),
        }),

    deleteToolServer: (serverId: string) =>
        fetchApi<void>(`/v1/tool-servers/${encodeURIComponent(serverId)}`, {
            method: 'DELETE',
        }),

    discoverToolServer: (serverId: string) =>
        fetchApi<ToolDiscoverResponse>(`/v1/tool-servers/${encodeURIComponent(serverId)}/discover`, {
            method: 'POST',
        }),

    // Memory (Phase 2 Track 5)
    listAgentMemory: (
        agentId: string,
        opts?: {
            outcome?: string;
            from?: string;
            to?: string;
            limit?: number;
            cursor?: string;
        }
    ) => {
        const params = new URLSearchParams();
        if (opts?.outcome) params.set('outcome', opts.outcome);
        if (opts?.from) params.set('from', opts.from);
        if (opts?.to) params.set('to', opts.to);
        if (opts?.limit) params.set('limit', opts.limit.toString());
        if (opts?.cursor) params.set('cursor', opts.cursor);
        const query = params.toString();
        return fetchApi<MemoryListResponse>(
            `/v1/agents/${encodeURIComponent(agentId)}/memory${query ? `?${query}` : ''}`
        );
    },

    searchAgentMemory: (
        agentId: string,
        query: string,
        opts?: {
            mode?: 'hybrid' | 'text' | 'vector';
            limit?: number;
            outcome?: string;
            from?: string;
            to?: string;
        }
    ) => {
        const params = new URLSearchParams();
        params.set('q', query);
        if (opts?.mode) params.set('mode', opts.mode);
        if (opts?.limit) params.set('limit', opts.limit.toString());
        if (opts?.outcome) params.set('outcome', opts.outcome);
        if (opts?.from) params.set('from', opts.from);
        if (opts?.to) params.set('to', opts.to);
        return fetchApi<MemorySearchResponse>(
            `/v1/agents/${encodeURIComponent(agentId)}/memory/search?${params.toString()}`
        );
    },

    getAgentMemoryEntry: (agentId: string, memoryId: string) =>
        fetchApi<MemoryEntryResponse>(
            `/v1/agents/${encodeURIComponent(agentId)}/memory/${encodeURIComponent(memoryId)}`
        ),

    deleteAgentMemoryEntry: (agentId: string, memoryId: string) =>
        fetchApi<void>(
            `/v1/agents/${encodeURIComponent(agentId)}/memory/${encodeURIComponent(memoryId)}`,
            { method: 'DELETE' }
        ),

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
        const baseUrl = import.meta.env.VITE_API_BASE_URL || '';
        const params = new URLSearchParams({ direction });
        return `${baseUrl}/v1/tasks/${taskId}/artifacts/${encodeURIComponent(filename)}?${params}`;
    },
};
