import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

import { useDashboardOverview } from './useDashboardOverview';

const useTaskListMock = vi.fn();
const useDeadLettersMock = vi.fn();
const getTaskStatusMock = vi.fn();

vi.mock('@/features/task-list/useTaskList', () => ({
    useTaskList: (...args: unknown[]) => useTaskListMock(...args),
}));

vi.mock('@/features/dead-letter/useDeadLetter', () => ({
    useDeadLetters: (...args: unknown[]) => useDeadLettersMock(...args),
}));

vi.mock('@/api/client', () => ({
    api: {
        getTaskStatus: (...args: unknown[]) => getTaskStatusMock(...args),
    },
}));

function createWrapper() {
    const client = new QueryClient({
        defaultOptions: {
            queries: {
                retry: false,
                gcTime: 0,
            },
        },
    });

    return function Wrapper({ children }: { children: ReactNode }) {
        return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    };
}

afterEach(() => {
    useTaskListMock.mockReset();
    useDeadLettersMock.mockReset();
    getTaskStatusMock.mockReset();
});

describe('useDashboardOverview', () => {
    it('enriches recent completed cost from task detail totals when the list fallback is zero', async () => {
        useTaskListMock.mockReturnValue({
            data: {
                items: [
                    {
                        task_id: 'fa705825-14bf-434b-a969-e94510186376',
                        agent_id: 'testttttt',
                        status: 'completed',
                        retry_count: 0,
                        checkpoint_count: 5,
                        total_cost_microdollars: 0,
                        created_at: '2026-03-28T04:47:59.29891Z',
                        updated_at: '2026-03-28T04:47:59.304002Z',
                    },
                ],
            },
            isLoading: false,
            isError: false,
        });

        useDeadLettersMock.mockReturnValue({
            data: { items: [] },
            isLoading: false,
            isError: false,
        });

        getTaskStatusMock.mockResolvedValue({
            task_id: 'fa705825-14bf-434b-a969-e94510186376',
            agent_id: 'testttttt',
            status: 'completed',
            input: 'What is 2+2?',
            output: { result: '4' },
            retry_count: 0,
            retry_history: [],
            checkpoint_count: 5,
            total_cost_microdollars: 2144,
            created_at: '2026-03-28T04:47:59.29891Z',
            updated_at: '2026-03-28T04:47:59.304002Z',
        });

        const { result } = renderHook(() => useDashboardOverview(), {
            wrapper: createWrapper(),
        });

        await waitFor(() => {
            expect(result.current.summary.recentCostMicrodollars).toBe(2144);
        });

        expect(result.current.recentRuns[0]?.total_cost_microdollars).toBe(2144);
    });
});
