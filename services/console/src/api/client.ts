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
    if (!headers.has('Content-Type') && options?.method !== 'GET' && options?.body) {
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

export const api = {
    submitTask: (request: TaskSubmissionRequest) =>
        fetchApi<TaskSubmissionResponse>('/v1/tasks', {
            method: 'POST',
            body: JSON.stringify({
                agent_id: request.agent_id,
                input: request.input,
                max_steps: request.max_steps,
                max_retries: request.max_retries,
                task_timeout_seconds: request.task_timeout_seconds,
                langfuse_endpoint_id: request.langfuse_endpoint_id,
            }),
        }),

    listTasks: (status?: string, agentId?: string, limit?: number) => {
        const params = new URLSearchParams();
        if (status) params.append('status', status);
        if (agentId) params.append('agent_id', agentId);
        if (limit) params.append('limit', limit.toString());
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
};
