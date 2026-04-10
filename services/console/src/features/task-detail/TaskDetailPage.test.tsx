import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TaskDetailPage } from './TaskDetailPage';

function createTestQueryClient() {
    return new QueryClient({
        defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
        },
    });
}

const navigateMock = vi.fn();
const redriveMutateMock = vi.fn();

vi.mock('react-router', async () => {
    const actual = await vi.importActual<typeof import('react-router')>('react-router');
    return {
        ...actual,
        useNavigate: () => navigateMock,
    };
});

const DEAD_LETTER_TASK = {
    task_id: 'task-1',
    agent_id: 'agent-1',
    status: 'dead_letter' as const,
    input: 'demo input',
    output: null,
    retry_count: 2,
    retry_history: ['2026-03-11T00:00:03Z', '2026-03-11T00:00:05Z'],
    checkpoint_count: 3,
    total_cost_microdollars: 0,
    lease_owner: null,
    last_error_code: 'retryable_error',
    last_error_message: 'forced failure',
    last_worker_id: 'worker-1',
    dead_letter_reason: 'retries_exhausted',
    dead_lettered_at: '2026-03-11T00:00:00Z',
    created_at: '2026-03-11T00:00:00Z',
    updated_at: '2026-03-11T00:00:00Z',
};

const taskStatusMock = vi.fn();
taskStatusMock.mockReturnValue({
    data: DEAD_LETTER_TASK,
    isLoading: false,
    isError: false,
});

vi.mock('./useTaskStatus', () => ({
    useTaskStatus: (...args: unknown[]) => taskStatusMock(...args),
    useCancelTask: () => ({
        mutate: vi.fn(),
        isPending: false,
    }),
}));

vi.mock('./useCheckpoints', () => ({
    useCheckpoints: () => ({
        data: {
            checkpoints: [
                {
                    checkpoint_id: 'cp-1',
                    task_id: 'task-1',
                    step_number: 1,
                    node_name: 'input',
                    worker_id: 'worker-1',
                    cost_microdollars: 0,
                    execution_metadata: null,
                    created_at: '2026-03-11T00:00:01Z',
                    event: {
                        type: 'input',
                        title: 'User Input',
                        summary: 'demo input',
                        content: 'demo input',
                        tool_name: null,
                        tool_args: null,
                        tool_result: null,
                        usage: null,
                    },
                },
                {
                    checkpoint_id: 'cp-2',
                    task_id: 'task-1',
                    step_number: 2,
                    node_name: 'loop',
                    worker_id: 'worker-1',
                    cost_microdollars: 0,
                    execution_metadata: null,
                    created_at: '2026-03-11T00:00:02Z',
                    event: {
                        type: 'tool_call',
                        title: 'Tool Call: read_url',
                        summary: 'agent called read_url',
                        content: null,
                        tool_name: 'read_url',
                        tool_args: { url: 'https://example.com' },
                        tool_result: null,
                        usage: null,
                    },
                },
                {
                    checkpoint_id: 'cp-3',
                    task_id: 'task-1',
                    step_number: 3,
                    node_name: 'loop',
                    worker_id: 'worker-1',
                    cost_microdollars: 0,
                    execution_metadata: null,
                    created_at: '2026-03-11T00:00:04Z',
                    event: {
                        type: 'tool_result',
                        title: 'Tool Result: read_url',
                        summary: 'read_url returned content',
                        content: null,
                        tool_name: 'read_url',
                        tool_args: null,
                        tool_result: { title: 'Example Domain' },
                        usage: null,
                    },
                },
            ],
        },
    }),
}));

vi.mock('./useTaskObservability', () => ({
    useTaskObservability: () => ({
        data: {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'dead_letter',
            trace_id: 'trace-1',
            total_cost_microdollars: 1500,
            input_tokens: 100,
            output_tokens: 50,
            total_tokens: 150,
            duration_ms: 2200,
            spans: [
                {
                    span_id: 'span-1',
                    parent_span_id: null,
                    task_id: 'task-1',
                    agent_id: 'agent-1',
                    actor_id: null,
                    type: 'tool',
                    node_name: 'loop',
                    model_name: null,
                    tool_name: 'read_url',
                    cost_microdollars: 1500,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: 500,
                    input: { url: 'https://example.com' },
                    output: { title: 'Example Domain' },
                    started_at: '2026-03-11T00:00:02Z',
                    ended_at: '2026-03-11T00:00:02.500Z',
                },
            ],
            items: [
                {
                    item_id: 'checkpoint-1',
                    parent_item_id: null,
                    kind: 'checkpoint_persisted',
                    title: 'Checkpoint saved',
                    summary: 'Saved durable progress at step 1.',
                    step_number: 1,
                    node_name: 'input',
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:01Z',
                    ended_at: null,
                },
                {
                    item_id: 'span-1',
                    parent_item_id: null,
                    kind: 'tool_span',
                    title: 'Tool: read_url',
                    summary: 'read_url returned content',
                    step_number: 2,
                    node_name: 'loop',
                    tool_name: 'read_url',
                    model_name: null,
                    cost_microdollars: 1500,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: 500,
                    input: { url: 'https://example.com' },
                    output: { title: 'Example Domain' },
                    started_at: '2026-03-11T00:00:02Z',
                    ended_at: '2026-03-11T00:00:02.500Z',
                },
                {
                    item_id: 'resume-1',
                    parent_item_id: null,
                    kind: 'resumed_after_retry',
                    title: 'Resumed from saved progress',
                    summary: 'Execution continued from the checkpoint saved after step 2.',
                    step_number: 2,
                    node_name: 'loop',
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:04Z',
                    ended_at: null,
                },
                {
                    item_id: 'dead-letter-1',
                    parent_item_id: null,
                    kind: 'dead_lettered',
                    title: 'Execution failed',
                    summary: 'A later attempt failed before another checkpoint could be saved.',
                    step_number: 3,
                    node_name: 'loop',
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:06Z',
                    ended_at: null,
                },
            ],
        },
    }),
}));

vi.mock('./CostSummary', () => ({
    CostSummary: ({ checkpointCount }: { checkpointCount: number }) => (
        <div>CostSummary checkpoints={checkpointCount}</div>
    ),
}));

vi.mock('@/features/dead-letter/useDeadLetter', () => ({
    useRedriveTask: () => ({
        mutate: redriveMutateMock,
        isPending: false,
    }),
}));

vi.mock('@/features/settings/useLangfuseEndpoints', () => ({
    useLangfuseEndpoints: () => ({
        data: [],
    }),
}));

vi.mock('sonner', () => ({
    toast: {
        success: vi.fn(),
        error: vi.fn(),
    },
}));

vi.mock('@/api/client', async () => {
    const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client');
    return {
        ...actual,
        api: {
            ...actual.api,
            followUpTask: vi.fn(),
            getTaskEvents: vi.fn().mockResolvedValue({ events: [] }),
        },
    };
});

afterEach(() => {
    cleanup();
    navigateMock.mockReset();
    redriveMutateMock.mockReset();
    taskStatusMock.mockReturnValue({
        data: DEAD_LETTER_TASK,
        isLoading: false,
        isError: false,
    });
});

function renderTaskDetail() {
    const queryClient = createTestQueryClient();
    render(
        <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={['/tasks/task-1']}>
                <Routes>
                    <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                </Routes>
            </MemoryRouter>
        </QueryClientProvider>,
    );
}

describe('TaskDetailPage', () => {
    it('navigates to the returned task after redrive succeeds', () => {
        redriveMutateMock.mockImplementation((_taskId, options) => {
            options?.onSuccess?.({ task_id: 'task-2', status: 'queued' });
        });

        renderTaskDetail();

        fireEvent.click(screen.getByRole('button', { name: 'Redrive Task' }));

        expect(navigateMock).toHaveBeenCalledWith('/tasks/task-2');
    });

    it('renders persisted checkpoints for dead-lettered tasks', () => {
        renderTaskDetail();

        expect(screen.getByText('Execution Failure')).toBeInTheDocument();
        expect(screen.getByText('CostSummary checkpoints=3')).toBeInTheDocument();
    });

    it('does not show follow-up panel for dead_letter tasks', () => {
        renderTaskDetail();

        expect(screen.queryByText('Follow Up')).not.toBeInTheDocument();
    });

    it('shows follow-up button for completed tasks', () => {
        taskStatusMock.mockReturnValue({
            data: {
                ...DEAD_LETTER_TASK,
                status: 'completed',
                output: '{"result": "done"}',
                dead_letter_reason: undefined,
                dead_lettered_at: undefined,
                last_error_code: undefined,
                last_error_message: undefined,
            },
            isLoading: false,
            isError: false,
        });

        renderTaskDetail();

        expect(screen.getByRole('button', { name: /follow up/i })).toBeInTheDocument();
    });
});

