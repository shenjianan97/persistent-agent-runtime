import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ActivityPane } from '../ActivityPane';
import type { ActivityEvent, ActivityListResponse } from '@/types';

const listActivityMock = vi.fn();

vi.mock('@/api/client', () => ({
    api: {
        listActivity: (...args: unknown[]) => listActivityMock(...args),
    },
    ApiError: class ApiError extends Error {
        status: number;
        constructor(status: number, message: string) {
            super(message);
            this.status = status;
        }
    },
}));

function event(partial: Partial<ActivityEvent> & Pick<ActivityEvent, 'kind'>): ActivityEvent {
    return {
        timestamp: '2026-04-20T00:00:00+00:00',
        ...partial,
    } as ActivityEvent;
}

const FIXTURE: ActivityListResponse = {
    events: [
        event({
            kind: 'turn.user',
            timestamp: '2026-04-20T00:00:00+00:00',
            role: 'user',
            content: 'Please list files',
        }),
        event({
            kind: 'turn.assistant',
            timestamp: '2026-04-20T00:00:01+00:00',
            role: 'assistant',
            content: 'Sure',
            tool_calls: [{ id: 'call_1', name: 'ls', args: { path: '/tmp' } }],
        }),
        event({
            kind: 'turn.tool',
            timestamp: '2026-04-20T00:00:02+00:00',
            role: 'tool',
            tool_name: 'ls',
            tool_call_id: 'call_1',
            content: 'file1\nfile2',
            is_error: false,
        }),
        event({
            kind: 'marker.compaction_fired',
            timestamp: '2026-04-20T00:00:03+00:00',
            event_type: 'task_compaction_fired',
            summary_text: 'Earlier turns summarised.',
            details: { tokens_in: 1000, tokens_out: 200, turns_summarized: 6 },
        }),
        event({
            kind: 'marker.hitl.paused',
            timestamp: '2026-04-20T00:00:04+00:00',
            event_type: 'task_paused',
            status_before: 'running',
            status_after: 'paused',
            details: { reason: 'tool_requires_approval', tool_name: 'delete_file' },
        }),
    ],
    next_cursor: null,
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
    listActivityMock.mockReset();
});

afterEach(() => {
    cleanup();
});

describe('ActivityPane', () => {
    it('renders turn kinds + markers from the API response', async () => {
        listActivityMock.mockResolvedValue(FIXTURE);
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() => expect(screen.getByTestId('activity-pane')).toBeInTheDocument());

        await waitFor(() => expect(screen.queryByTestId('activity-loading')).not.toBeInTheDocument());

        // Each event renders a row with a data-kind attribute — we assert the
        // kind bucket and the role-anchored body content.
        expect(screen.getByTestId('activity-row-0')).toHaveAttribute('data-kind', 'turn.user');
        expect(screen.getByTestId('activity-row-0-content')).toHaveTextContent('Please list files');

        expect(screen.getByTestId('activity-row-1')).toHaveAttribute('data-kind', 'turn.assistant');
        expect(screen.getByTestId('activity-row-1-content')).toHaveTextContent('Sure');
        // Tool-call folds share the containing assistant turn's
        // timestamp (they were emitted in the same AIMessage), but we
        // surface it regardless for visual parity with tool-result folds.
        expect(screen.getByTestId('activity-row-1-tool-call-0-timestamp')).toBeInTheDocument();

        expect(screen.getByTestId('activity-row-2')).toHaveAttribute('data-kind', 'turn.tool');
        expect(screen.getByTestId('activity-row-2-content')).toHaveTextContent('file1');
        // Tool results carry their own per-message timestamp (checkpoint
        // where the ToolMessage first appeared), not the containing
        // assistant turn's timestamp — surfaced on the fold label.
        expect(screen.getByTestId('activity-row-2-timestamp')).toBeInTheDocument();

        expect(screen.getByTestId('activity-row-3')).toHaveAttribute('data-kind', 'marker.compaction_fired');
        // Summary text body is visible even without the details toggle.
        expect(screen.getByTestId('activity-row-3')).toHaveTextContent('Earlier turns summarised.');

        expect(screen.getByTestId('activity-row-4')).toHaveAttribute('data-kind', 'marker.hitl.paused');
    });

    it('flips include_details query param when the toggle is clicked', async () => {
        listActivityMock.mockResolvedValue({ events: [], next_cursor: null });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() => expect(listActivityMock).toHaveBeenCalledWith('task-1', false));

        const toggle = await screen.findByTestId('activity-details-toggle');
        fireEvent.click(toggle);

        await waitFor(() => expect(listActivityMock).toHaveBeenCalledWith('task-1', true));
    });

    it('expands a row to reveal raw details on click', async () => {
        listActivityMock.mockResolvedValue(FIXTURE);
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() => expect(screen.getByTestId('activity-row-3')).toBeInTheDocument());

        // Row 3 = compaction_fired has details (tokens_in, tokens_out, ...).
        const expandBtn = screen.getByTestId('activity-row-3-expand');
        fireEvent.click(expandBtn);

        const detailsBlock = await screen.findByTestId('activity-row-3-details');
        expect(detailsBlock).toHaveTextContent('tokens_in');
        expect(detailsBlock).toHaveTextContent('turns_summarized');
    });

    it('shows empty-state when the API returns no events', async () => {
        listActivityMock.mockResolvedValue({ events: [], next_cursor: null });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() => expect(screen.getByTestId('activity-empty')).toBeInTheDocument());
    });

    it('shows error banner when the API rejects', async () => {
        listActivityMock.mockRejectedValue(new Error('boom'));
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        const err = await screen.findByTestId('activity-error');
        expect(err).toHaveTextContent('Failed to load activity');
        expect(err).toHaveTextContent('boom');
    });

    it('surfaces the tool-result error flag visually', async () => {
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'turn.tool',
                    tool_name: 'bad_tool',
                    tool_call_id: 'c1',
                    content: 'stack trace',
                    is_error: true,
                }),
            ],
            next_cursor: null,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        const row = await screen.findByTestId('activity-row-0');
        expect(row).toHaveTextContent('bad_tool');
        expect(row).toHaveTextContent('error');
    });
});
