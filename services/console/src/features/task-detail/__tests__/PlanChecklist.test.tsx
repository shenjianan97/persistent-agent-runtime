import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PlanChecklist } from '../PlanChecklist';
import type { TaskPlanResponse } from '@/types';

const getTaskPlanMock = vi.fn();

vi.mock('@/api/client', () => ({
    api: {
        getTaskPlan: (...args: unknown[]) => getTaskPlanMock(...args),
    },
    ApiError: class ApiError extends Error {
        status: number;
        constructor(status: number, message: string) {
            super(message);
            this.status = status;
        }
    },
}));

const POPULATED_PLAN: TaskPlanResponse = {
    task_id: 'task-123',
    plan: [
        { id: 'p1', title: 'Research the topic', status: 'completed' },
        { id: 'p2', title: 'Write outline', status: 'in_progress' },
        { id: 'p3', title: 'Draft section 1', status: 'pending' },
    ],
    updated_at: '2026-06-01T10:00:00Z',
};

const EMPTY_PLAN: TaskPlanResponse = {
    task_id: 'task-456',
    plan: [],
};

function renderWithClient(ui: React.ReactElement) {
    const client = new QueryClient({
        defaultOptions: {
            queries: { retry: false },
        },
    });
    return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
    getTaskPlanMock.mockReset();
});

afterEach(() => {
    cleanup();
});

describe('PlanChecklist', () => {
    it('renders one checkbox row per item in API order with correct data-testids', async () => {
        getTaskPlanMock.mockResolvedValue(POPULATED_PLAN);
        renderWithClient(<PlanChecklist taskId="task-123" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        expect(screen.getByTestId('plan-item-p1')).toBeInTheDocument();
        expect(screen.getByTestId('plan-item-p2')).toBeInTheDocument();
        expect(screen.getByTestId('plan-item-p3')).toBeInTheDocument();

        // API order preserved — use a regex that excludes badge testids (-badge suffix)
        const items = screen.getAllByTestId(/^plan-item-(?!.*-badge$)/);
        expect(items).toHaveLength(3);
        expect(items[0]).toHaveAttribute('data-testid', 'plan-item-p1');
        expect(items[1]).toHaveAttribute('data-testid', 'plan-item-p2');
        expect(items[2]).toHaveAttribute('data-testid', 'plan-item-p3');
    });

    it('renders title text for each item', async () => {
        getTaskPlanMock.mockResolvedValue(POPULATED_PLAN);
        renderWithClient(<PlanChecklist taskId="task-123" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        expect(screen.getByTestId('plan-item-p1')).toHaveTextContent('Research the topic');
        expect(screen.getByTestId('plan-item-p2')).toHaveTextContent('Write outline');
        expect(screen.getByTestId('plan-item-p3')).toHaveTextContent('Draft section 1');
    });

    it('checks completed items and leaves others unchecked', async () => {
        getTaskPlanMock.mockResolvedValue(POPULATED_PLAN);
        renderWithClient(<PlanChecklist taskId="task-123" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        // completed → checked
        const completedRow = screen.getByTestId('plan-item-p1');
        const completedCheckbox = completedRow.querySelector('input[type="checkbox"]');
        expect(completedCheckbox).toBeChecked();

        // in_progress → unchecked
        const inProgressRow = screen.getByTestId('plan-item-p2');
        const inProgressCheckbox = inProgressRow.querySelector('input[type="checkbox"]');
        expect(inProgressCheckbox).not.toBeChecked();

        // pending → unchecked
        const pendingRow = screen.getByTestId('plan-item-p3');
        const pendingCheckbox = pendingRow.querySelector('input[type="checkbox"]');
        expect(pendingCheckbox).not.toBeChecked();
    });

    it('renders status badges distinguishing all three statuses', async () => {
        getTaskPlanMock.mockResolvedValue(POPULATED_PLAN);
        renderWithClient(<PlanChecklist taskId="task-123" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        expect(screen.getByTestId('plan-item-p1-badge')).toHaveTextContent('completed');
        expect(screen.getByTestId('plan-item-p2-badge')).toHaveTextContent('in progress');
        expect(screen.getByTestId('plan-item-p3-badge')).toHaveTextContent('pending');
    });

    it('renders nothing (or empty state) for an empty plan without error', async () => {
        getTaskPlanMock.mockResolvedValue(EMPTY_PLAN);
        renderWithClient(<PlanChecklist taskId="task-456" />);

        // Container still renders (or is absent) — no error thrown
        await waitFor(() => expect(getTaskPlanMock).toHaveBeenCalledWith('task-456'));

        // No plan item rows (exclude badge testids)
        expect(screen.queryAllByTestId(/^plan-item-(?!.*-badge$)/)).toHaveLength(0);
    });

    it('checkboxes are read-only (disabled, no interactive toggle)', async () => {
        getTaskPlanMock.mockResolvedValue(POPULATED_PLAN);
        renderWithClient(<PlanChecklist taskId="task-123" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        const checkboxes = screen.getAllByRole('checkbox');
        expect(checkboxes).toHaveLength(3);
        for (const checkbox of checkboxes) {
            expect(checkbox).toBeDisabled();
        }
    });

    it('tolerates null status without crashing and degrades badge to pending-like', async () => {
        const planWithNullStatus: TaskPlanResponse = {
            task_id: 'task-789',
            plan: [
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                { id: 'x1', title: 'Some item', status: null as any },
            ],
        };
        getTaskPlanMock.mockResolvedValue(planWithNullStatus);
        renderWithClient(<PlanChecklist taskId="task-789" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        expect(screen.getByTestId('plan-item-x1')).toBeInTheDocument();
        // Badge renders without crashing (content may be 'pending' or similar neutral text)
        const badge = screen.getByTestId('plan-item-x1-badge');
        expect(badge).toBeInTheDocument();
        // Checkbox is unchecked (null status is not "completed")
        const checkbox = screen.getByTestId('plan-item-x1').querySelector('input[type="checkbox"]');
        expect(checkbox).not.toBeChecked();
    });

    it('tolerates unknown status value without crashing', async () => {
        const planWithUnknownStatus: TaskPlanResponse = {
            task_id: 'task-aaa',
            plan: [
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                { id: 'y1', title: 'Unknown status item', status: 'unknown_future_value' as any },
            ],
        };
        getTaskPlanMock.mockResolvedValue(planWithUnknownStatus);
        renderWithClient(<PlanChecklist taskId="task-aaa" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        expect(screen.getByTestId('plan-item-y1')).toBeInTheDocument();
        const badge = screen.getByTestId('plan-item-y1-badge');
        expect(badge).toBeInTheDocument();
        const checkbox = screen.getByTestId('plan-item-y1').querySelector('input[type="checkbox"]');
        expect(checkbox).not.toBeChecked();
    });

    it('tolerates null id by falling back to index-based key without crashing', async () => {
        const planWithNullId: TaskPlanResponse = {
            task_id: 'task-bbb',
            plan: [
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                { id: null as any, title: 'Item with null id', status: 'pending' },
            ],
        };
        getTaskPlanMock.mockResolvedValue(planWithNullId);
        renderWithClient(<PlanChecklist taskId="task-bbb" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        // Does not crash; renders at least one row with a fallback testid (exclude badge)
        const rows = screen.queryAllByTestId(/^plan-item-(?!.*-badge$)/);
        expect(rows).toHaveLength(1);
    });

    it('tolerates null title by rendering empty or fallback text without crashing', async () => {
        const planWithNullTitle: TaskPlanResponse = {
            task_id: 'task-ccc',
            plan: [
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                { id: 'z1', title: null as any, status: 'pending' },
            ],
        };
        getTaskPlanMock.mockResolvedValue(planWithNullTitle);
        renderWithClient(<PlanChecklist taskId="task-ccc" />);

        await waitFor(() => expect(screen.getByTestId('plan-checklist')).toBeInTheDocument());

        expect(screen.getByTestId('plan-item-z1')).toBeInTheDocument();
        // Should not throw
    });

    it('calls getTaskPlan with the correct taskId', async () => {
        getTaskPlanMock.mockResolvedValue(POPULATED_PLAN);
        renderWithClient(<PlanChecklist taskId="task-xyz" />);

        await waitFor(() => expect(getTaskPlanMock).toHaveBeenCalledWith('task-xyz'));
    });
});
