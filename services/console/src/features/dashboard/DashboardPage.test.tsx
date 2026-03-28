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

        const { container } = render(
            <MemoryRouter>
                <DashboardPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('Home')).toBeInTheDocument();
        expect(screen.getByRole('link', { name: /submit task/i })).toHaveAttribute('href', '/tasks/new');
        expect(screen.getAllByRole('link', { name: /view all tasks/i })).toHaveLength(2);
        expect(screen.getByText('Needs Attention')).toBeInTheDocument();
        expect(screen.getByText('Queued + Running')).toBeInTheDocument();
        expect(screen.getByText('Recent Runs')).toBeInTheDocument();
        expect(screen.getByText('retries_exhausted')).toBeInTheDocument();
        expect(screen.getAllByText(/support-agent/i).length).toBeGreaterThan(0);
        expect(screen.getAllByText('$0.0052').length).toBeGreaterThan(0);
        expect(screen.queryByText('No tasks are currently running.')).not.toBeInTheDocument();
        expect(container.querySelector('.tabular-nums')).not.toBeNull();
    });

    it('uses restrained metric sizing in summary cards', () => {
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

        const metricValue = screen.getAllByText('0')[0];
        expect(metricValue.className).toContain('text-3xl');
        expect(metricValue.className).not.toContain('text-4xl');
        expect(metricValue.parentElement?.className).not.toContain('bg-black/10');
    });

    it('keeps summary cards visually flat instead of nesting pill containers', () => {
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

        const title = screen.getByText('Queued + Running');
        const summaryCard = title.closest('.console-surface');

        expect(summaryCard).not.toBeNull();
        expect(summaryCard?.querySelector('.rounded-full')).toBeNull();
    });

    it('uses a consistent non-display font treatment for dashboard headings', () => {
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

        expect(screen.getByRole('heading', { name: 'Home' }).className).not.toContain('font-display');
        expect(screen.getByRole('heading', { name: 'Recent Runs' }).className).not.toContain('font-display');
    });

    it('uses the same readable body style for summary and section descriptions', () => {
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

        const summaryCopy = screen.getByText('Active tasks are shown as a quick signal, not as a separate empty panel.');
        expect(summaryCopy.className).toContain('text-sm');
        expect(summaryCopy.className).not.toContain('text-[11px]');
        expect(summaryCopy.className).not.toContain('tracking-[0.04em]');
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
        expect(screen.getByText('Submit your first task to start building execution history.')).toBeInTheDocument();
    });

    it('shows Home and Failed in the primary navigation', () => {
        render(
            <MemoryRouter>
                <Sidebar />
            </MemoryRouter>,
        );

        expect(screen.getByRole('link', { name: /home/i })).toHaveAttribute('href', '/');
        expect(screen.getByRole('link', { name: /failed/i })).toHaveAttribute('href', '/dead-letter');
        expect(screen.queryByRole('link', { name: /overview/i })).not.toBeInTheDocument();
        expect(screen.queryByRole('link', { name: /dead letters/i })).not.toBeInTheDocument();
    });
});
