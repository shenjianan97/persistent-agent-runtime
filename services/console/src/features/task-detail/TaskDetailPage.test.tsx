import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { TaskDetailPage } from './TaskDetailPage';

vi.mock('./useTaskStatus', () => ({
    useTaskStatus: () => ({
        data: {
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'dead_letter',
            input: 'demo input',
            output: null,
            retry_count: 0,
            retry_history: [],
            checkpoint_count: 2,
            total_cost_microdollars: 0,
            lease_owner: null,
            last_error_code: 'non_retryable_error',
            last_error_message: 'forced failure',
            last_worker_id: 'worker-1',
            dead_letter_reason: 'non_retryable_error',
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
        mutate: vi.fn(),
        isPending: false,
    }),
}));

vi.mock('sonner', () => ({
    toast: {
        success: vi.fn(),
        error: vi.fn(),
    },
}));

describe('TaskDetailPage', () => {
    it('renders persisted checkpoints for dead-lettered tasks', () => {
        render(
            <MemoryRouter initialEntries={['/tasks/task-1']}>
                <Routes>
                    <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                </Routes>
            </MemoryRouter>,
        );

        expect(screen.getByText('Execution Failure')).toBeInTheDocument();
        expect(screen.getByText('CostSummary checkpoints=2')).toBeInTheDocument();
        expect(screen.getByText('Tool Call: read_url')).toBeInTheDocument();
        expect(screen.queryByText('Waiting for checkpoints...')).not.toBeInTheDocument();
    });
});
