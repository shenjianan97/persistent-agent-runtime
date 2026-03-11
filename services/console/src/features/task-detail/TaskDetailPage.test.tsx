import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TaskDetailPage } from './TaskDetailPage';

const navigateMock = vi.fn();
const redriveMutateMock = vi.fn();

vi.mock('react-router', async () => {
    const actual = await vi.importActual<typeof import('react-router')>('react-router');
    return {
        ...actual,
        useNavigate: () => navigateMock,
    };
});

vi.mock('./useTaskStatus', () => ({
    useTaskStatus: () => ({
        data: {
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'dead_letter',
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
        },
        isLoading: false,
        isError: false,
    }),
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

vi.mock('./CostSummary', () => ({
    CostSummary: ({ checkpoints }: { checkpoints: Array<{ checkpoint_id: string }> }) => (
        <div>CostSummary checkpoints={checkpoints.length}</div>
    ),
}));

vi.mock('@/features/dead-letter/useDeadLetter', () => ({
    useRedriveTask: () => ({
        mutate: redriveMutateMock,
        isPending: false,
    }),
}));

vi.mock('sonner', () => ({
    toast: {
        success: vi.fn(),
        error: vi.fn(),
    },
}));

afterEach(() => {
    cleanup();
    navigateMock.mockReset();
    redriveMutateMock.mockReset();
});

describe('TaskDetailPage', () => {
    it('navigates to the returned task after redrive succeeds', () => {
        redriveMutateMock.mockImplementation((_taskId, options) => {
            options?.onSuccess?.({ task_id: 'task-2', status: 'queued' });
        });

        render(
            <MemoryRouter initialEntries={['/tasks/task-1']}>
                <Routes>
                    <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                </Routes>
            </MemoryRouter>,
        );

        fireEvent.click(screen.getByRole('button', { name: 'Redrive Task' }));

        expect(navigateMock).toHaveBeenCalledWith('/tasks/task-2');
    });

    it('renders persisted checkpoints for dead-lettered tasks', () => {
        render(
            <MemoryRouter initialEntries={['/tasks/task-1']}>
                <Routes>
                    <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                </Routes>
            </MemoryRouter>,
        );

        expect(screen.getByText('Execution Failure')).toBeInTheDocument();
        expect(screen.getByText('CostSummary checkpoints=3')).toBeInTheDocument();
        expect(screen.getByText('Tool Call: read_url')).toBeInTheDocument();
        expect(screen.getByText('Resumed From Saved Progress')).toBeInTheDocument();
        expect(screen.getByText('Execution continued from the checkpoint saved after step 2, so earlier progress was preserved.')).toBeInTheDocument();
        expect(screen.getByText('Execution Failed')).toBeInTheDocument();
        expect(screen.getByText('A later attempt failed before another checkpoint could be saved, so the timeline ends at the last durable step below.')).toBeInTheDocument();
        expect(screen.getByText('Last durable checkpoint: step 3')).toBeInTheDocument();
        expect(screen.getByText('Error code: retryable_error')).toBeInTheDocument();
        expect(screen.queryByText('Waiting for checkpoints...')).not.toBeInTheDocument();
    });
});
