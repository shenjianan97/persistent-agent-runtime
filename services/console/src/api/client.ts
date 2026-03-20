import {
    TaskStatusResponse,
    TaskSubmissionRequest,
    TaskSubmissionResponse,
    TaskListResponse,
    CheckpointListResponse,
    DeadLetterListResponse,
    HealthResponse,
    TaskCancelResponse,
    RedriveResponse,
    ModelResponse
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
    submitTask: (request: TaskSubmissionRequest) => {
        // Map flat frontend form shape to Java backend nested shape
        const payload = {
            agent_id: request.agent_id,
            input: request.input,
            max_steps: request.max_steps,
            max_retries: request.max_retries,
            task_timeout_seconds: request.task_timeout_seconds,
            agent_config: {
                system_prompt: request.system_prompt,
                provider: request.provider,
                model: request.model,
                temperature: request.temperature,
                allowed_tools: request.allowed_tools
            }
        };

        return fetchApi<TaskSubmissionResponse>('/v1/tasks', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
    },

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

    getHealth: () =>
        fetchApi<HealthResponse>('/v1/health'),

    getModels: () =>
        fetchApi<ModelResponse[]>('/v1/models'),
};
