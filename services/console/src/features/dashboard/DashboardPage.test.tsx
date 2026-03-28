import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DashboardPage } from './DashboardPage';
import { Sidebar } from '@/layout/Sidebar';

const overviewMock = vi.fn();

vi.mock('./useDashboardOverview', () => ({
    useDashboardOverview: () => overviewMock(),
}));

afterEach(() => {
    cleanup();
    overviewMock.mockReset();
});

describe('DashboardPage', () => {
    it('renders summary cards and actionable sections from task data', () => {
        overviewMock.mockReturnValue({
            isLoading: false,
            isError: false,
            deadLetters: [
                {
                    task_id: 'deadbeef-0000-0000-0000-000000000001',
                    agent_id: 'research-agent',
                    dead_letter_reason: 'retries_exhausted',
                    last_error_message: 'provider timeout',
                    retry_count: 2,
                    dead_lettered_at: '2026-03-27T19:00:00Z',
                },
            ],
            inProgress: [
                {
                    task_id: 'feedface-0000-0000-0000-000000000002',
                    agent_id: 'support-agent',
                    status: 'running',
                    retry_count: 0,
                    checkpoint_count: 3,
                    total_cost_microdollars: 2100,
                    created_at: '2026-03-27T18:55:00Z',
                    updated_at: '2026-03-27T19:05:00Z',
                },
            ],
            recentRuns: [
                {
                    task_id: '8badf00d-0000-0000-0000-000000000003',
                    agent_id: 'support-agent',
                    status: 'completed',
                    retry_count: 0,
                    checkpoint_count: 5,
                    total_cost_microdollars: 5200,
                    created_at: '2026-03-27T18:20:00Z',
                    updated_at: '2026-03-27T18:25:00Z',
                },
            ],
            summary: {
                inProgressCount: 1,
                deadLetterCount: 1,
                completedCount: 1,
                recentCostMicrodollars: 5200,
            },
        });

        render(
            <MemoryRouter>
                <DashboardPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('Home')).toBeInTheDocument();
        expect(screen.getByRole('link', { name: /submit task/i })).toHaveAttribute('href', '/tasks/new');
        expect(screen.getAllByRole('link', { name: /view all tasks/i })).toHaveLength(2);
        expect(screen.getByText('Needs Attention')).toBeInTheDocument();
        expect(screen.getAllByText('In Progress')).toHaveLength(2);
        expect(screen.getByText('Recent Runs')).toBeInTheDocument();
        expect(screen.getByText('retries_exhausted')).toBeInTheDocument();
        expect(screen.getAllByText(/support-agent/i).length).toBeGreaterThan(0);
        expect(screen.getAllByText('$0.0052').length).toBeGreaterThan(0);
    });

    it('renders encouraging empty states when no runs are available', () => {
        overviewMock.mockReturnValue({
            isLoading: false,
            isError: false,
            deadLetters: [],
            inProgress: [],
            recentRuns: [],
            summary: {
                inProgressCount: 0,
                deadLetterCount: 0,
                completedCount: 0,
                recentCostMicrodollars: 0,
            },
        });

        render(
            <MemoryRouter>
                <DashboardPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('No runs need attention right now.')).toBeInTheDocument();
        expect(screen.getByText('No tasks are currently running.')).toBeInTheDocument();
        expect(screen.getByText('Submit your first task to start building execution history.')).toBeInTheDocument();
    });

    it('shows Home in the primary navigation', () => {
        render(
            <MemoryRouter>
                <Sidebar />
            </MemoryRouter>,
        );

        expect(screen.getByRole('link', { name: /home/i })).toHaveAttribute('href', '/');
        expect(screen.queryByRole('link', { name: /overview/i })).not.toBeInTheDocument();
    });
});
