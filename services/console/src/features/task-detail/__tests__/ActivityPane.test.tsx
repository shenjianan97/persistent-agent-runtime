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

    it('renders per-turn duration on assistant and tool rows', async () => {
        listActivityMock.mockResolvedValue(FIXTURE);
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() =>
            expect(screen.getByTestId('activity-row-1')).toBeInTheDocument(),
        );
        // Row 0 has no predecessor → no duration. Rows 1 + 2 do.
        expect(screen.getByTestId('activity-row-1-duration')).toHaveTextContent('Δ');
        expect(screen.getByTestId('activity-row-2-duration')).toHaveTextContent('Δ');
    });

    it('renders cumulative assistant cost once there is more than one assistant turn', async () => {
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:00+00:00',
                    content: 'first',
                    cost_microdollars: 120_000,
                }),
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:10+00:00',
                    content: 'second',
                    cost_microdollars: 330_000,
                }),
            ],
            next_cursor: null,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() =>
            expect(screen.getByTestId('activity-row-1')).toBeInTheDocument(),
        );
        // First assistant turn has cumulative == per-turn cost → no chip.
        expect(
            screen.queryByTestId('activity-row-0-cumulative-cost'),
        ).not.toBeInTheDocument();
        // Second assistant turn has cumulative > current → chip appears.
        const chip = screen.getByTestId('activity-row-1-cumulative-cost');
        expect(chip).toHaveTextContent('so far');
    });

    it('renders a handoff banner when consecutive turns have different worker_ids', async () => {
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:00+00:00',
                    content: 'first',
                    worker_id: 'worker-aaaaaaaa-1',
                }),
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:05+00:00',
                    content: 'second',
                    worker_id: 'worker-bbbbbbbb-2',
                }),
            ],
            next_cursor: null,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        await waitFor(() =>
            expect(screen.getByTestId('activity-handoff-1')).toBeInTheDocument(),
        );
        expect(screen.getByTestId('activity-handoff-1')).toHaveTextContent('Handoff');
        // Worker ids truncated to first 8 chars.
        expect(screen.getByTestId('activity-handoff-1')).toHaveTextContent('worker-a');
        expect(screen.getByTestId('activity-handoff-1')).toHaveTextContent('worker-b');
    });

    it('promotes dead_lettered lifecycle events with destructive styling', async () => {
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'marker.lifecycle',
                    timestamp: '2026-04-20T00:00:00+00:00',
                    event_type: 'task_dead_lettered',
                    details: {
                        reason: 'tier3_tokens_out_exceeded',
                        error_code: 'TIER3_TOKENS_OUT_EXCEEDED',
                    },
                }),
            ],
            next_cursor: null,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="dead_letter" />);

        const row = await screen.findByTestId('activity-row-0');
        expect(row).toHaveTextContent('Task failed');
        expect(row).toHaveTextContent('tier3_tokens_out_exceeded');
        // Destructive border class applied.
        expect(row.className).toMatch(/destructive/);
    });

    it('renders the truncation notice when the page is capped', async () => {
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'turn.user',
                    content: 'hello',
                }),
            ],
            next_cursor: null,
            truncated: true,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        const notice = await screen.findByTestId('activity-truncation-notice');
        expect(notice).toHaveTextContent('Showing first 2000 of many events');
    });

    it('renders a tool-only assistant turn without prose (empty content + tool_calls)', async () => {
        // Server now pre-normalizes message content, so a tool-only turn
        // arrives with `content: ''` and non-empty `tool_calls[]`. The row
        // must still expose the `activity-row-<i>-content` testid (sr-only)
        // for a11y consumers, and the tool-call fold must render.
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:00+00:00',
                    role: 'assistant',
                    content: '',
                    tool_calls: [
                        { id: 'call_1', name: 'web_search', args: { q: 'x' } },
                    ],
                }),
            ],
            next_cursor: null,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        const row = await screen.findByTestId('activity-row-0');
        expect(row).toHaveAttribute('data-kind', 'turn.assistant');
        // sr-only content element is present + has no child text (the prose
        // bubble must NOT render for a tool-only turn).
        const contentEl = screen.getByTestId('activity-row-0-content');
        expect(contentEl).toBeInTheDocument();
        expect(contentEl).toHaveClass('sr-only');
        expect(contentEl).toBeEmptyDOMElement();
        // Nor should the visible prose bubble container appear — asserting
        // the sr-only element exists without this check would pass against
        // an implementation that rendered both an sr-only hook AND a
        // visible empty bubble.
        expect(row.querySelector('.rounded-2xl')).toBeNull();
        // The tool-call fold renders (label contains the tool name).
        expect(row).toHaveTextContent('web_search');
    });

    it('refetches once when task status transitions from running to terminal', async () => {
        // Reproduces the race where the 3s activity poll happens before
        // the final checkpoint lands. The parent's status transition to a
        // terminal value also stops polling, so without a transition-fire
        // refetch the pane shows stale events missing the final turn.
        const runningResponse = {
            events: [
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:00+00:00',
                    content: 'thinking...',
                    tool_calls: [{ id: 'c1', name: 'web_search', args: {} }],
                }),
            ],
            next_cursor: null,
        };
        const completedResponse = {
            events: [
                ...runningResponse.events,
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-04-20T00:00:10+00:00',
                    content: 'Final answer.',
                }),
            ],
            next_cursor: null,
        };
        listActivityMock.mockImplementation((_taskId: string) =>
            Promise.resolve(
                listActivityMock.mock.calls.length === 1
                    ? runningResponse
                    : completedResponse,
            ),
        );

        const client = new QueryClient({
            defaultOptions: { queries: { retry: false } },
        });
        const { rerender } = render(
            <QueryClientProvider client={client}>
                <ActivityPane taskId="task-1" status="running" />
            </QueryClientProvider>,
        );

        await waitFor(() =>
            expect(screen.getByTestId('activity-row-0')).toBeInTheDocument(),
        );
        // Initial render has exactly one event — the final turn hasn't
        // landed yet.
        expect(screen.queryByTestId('activity-row-1')).not.toBeInTheDocument();

        // Simulate the parent observing status transition to terminal.
        rerender(
            <QueryClientProvider client={client}>
                <ActivityPane taskId="task-1" status="completed" />
            </QueryClientProvider>,
        );

        // After the transition the pane must refetch once and pick up the
        // final turn without a manual page refresh.
        await waitFor(() =>
            expect(screen.getByTestId('activity-row-1')).toBeInTheDocument(),
        );
        expect(screen.getByTestId('activity-row-1-content')).toHaveTextContent(
            'Final answer.',
        );
        // Exactly two calls — one on mount + one on the transition. No
        // additional poll cycles.
        expect(listActivityMock).toHaveBeenCalledTimes(2);
    });

    it('renders the byte-cap notice on tool rows when orig_bytes > content length', async () => {
        listActivityMock.mockResolvedValue({
            events: [
                event({
                    kind: 'turn.tool',
                    tool_name: 'read_file',
                    tool_call_id: 'c1',
                    content: 'shortoutput',
                    orig_bytes: 99999,
                }),
            ],
            next_cursor: null,
        });
        renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

        const notice = await screen.findByTestId('activity-row-0-byte-cap-notice');
        expect(notice).toHaveTextContent('99999');
        expect(notice).toHaveTextContent('head+tail capped view');
    });

    describe('sub-agent fan-out tree (S9)', () => {
        // Two iterations; subtask 1.1 fails in round 1 and is re-dispatched
        // (same id) in round 2 where it succeeds — the round-2 group must link
        // back to its round-1 failure via a retry chip.
        const TREE_FIXTURE: ActivityListResponse = {
            events: [
                event({
                    kind: 'turn.user',
                    timestamp: '2026-06-08T00:00:00+00:00',
                    role: 'user',
                    content: 'Research the topic',
                }),
                event({
                    kind: 'marker.supervisor.iteration',
                    timestamp: '2026-06-08T00:00:01+00:00',
                    event_type: 'supervisor_iteration',
                    iteration: 1,
                    details: { iteration: 1, subtasks_emitted: 2, decision: 'continue', reason: 'more sources' },
                }),
                event({
                    kind: 'marker.subagent.finding',
                    timestamp: '2026-06-08T00:00:02+00:00',
                    event_type: 'subagent_finding',
                    iteration: 1,
                    subtask: '1.0',
                    details: { iteration: 1, subtask: '1.0', finding_id: '1.0-abcd1234', source_url: 'https://example.com/a' },
                }),
                event({
                    kind: 'marker.subagent.failed',
                    timestamp: '2026-06-08T00:00:03+00:00',
                    event_type: 'subagent_failed',
                    iteration: 1,
                    subtask: '1.1',
                    details: { iteration: 1, subtask: '1.1', reason: 'tool transport error' },
                }),
                event({
                    kind: 'marker.supervisor.iteration',
                    timestamp: '2026-06-08T00:00:04+00:00',
                    event_type: 'supervisor_iteration',
                    iteration: 2,
                    details: { iteration: 2, subtasks_emitted: 1, decision: 'stop', reason: 'enough evidence' },
                }),
                event({
                    kind: 'marker.subagent.finding',
                    timestamp: '2026-06-08T00:00:05+00:00',
                    event_type: 'subagent_finding',
                    iteration: 2,
                    subtask: '1.1',
                    details: { iteration: 2, subtask: '1.1', finding_id: '1.1-ef567890', source_url: 'https://example.com/b' },
                }),
                event({
                    kind: 'turn.assistant',
                    timestamp: '2026-06-08T00:00:06+00:00',
                    role: 'assistant',
                    content: 'Here is the report.',
                }),
            ],
            next_cursor: null,
        };

        it('groups markers into round → sub-agent folds with the expected testids', async () => {
            listActivityMock.mockResolvedValue(TREE_FIXTURE);
            renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

            expect(await screen.findByTestId('activity-subagent-tree')).toBeInTheDocument();
            // Two round toggles.
            expect(screen.getByTestId('activity-round-1-toggle')).toBeInTheDocument();
            expect(screen.getByTestId('activity-round-2-toggle')).toBeInTheDocument();
            // Sub-agent group toggles per subtask per round (1.0 + 1.1 in round 1, 1.1 in round 2).
            const subagentToggles = screen.getAllByTestId('activity-subagent-1.1');
            expect(subagentToggles.length).toBe(2);
            expect(screen.getByTestId('activity-subagent-1.0')).toBeInTheDocument();
        });

        it('renders the round summary from the supervisor.iteration marker', async () => {
            listActivityMock.mockResolvedValue(TREE_FIXTURE);
            renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

            const round1 = await screen.findByTestId('activity-round-1-toggle');
            expect(round1).toHaveTextContent('Round 1');
            expect(round1).toHaveTextContent('continue');
            const round2 = screen.getByTestId('activity-round-2-toggle');
            expect(round2).toHaveTextContent('Round 2');
            expect(round2).toHaveTextContent('stop');
        });

        it('renders status chips and links a round-2 retry to its round-1 failure', async () => {
            listActivityMock.mockResolvedValue(TREE_FIXTURE);
            renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

            await screen.findByTestId('activity-subagent-tree');
            // The round-1 1.1 group is failed; the round-2 1.1 group is found and retried.
            const statusChips = screen.getAllByTestId('activity-subagent-1.1-status');
            const chipTexts = statusChips.map((c) => c.textContent);
            expect(chipTexts).toContain('failed');
            expect(chipTexts).toContain('finding');
            // Exactly one retry chip (on the round-2 group), pointing at round 1.
            const retry = screen.getByTestId('activity-subagent-1.1-retry');
            expect(retry).toHaveTextContent('retried from round 1');
        });

        it('renders finding leaves with finding_id + source_url and keeps non-sub-agent rows intact', async () => {
            listActivityMock.mockResolvedValue(TREE_FIXTURE);
            renderWithClient(<ActivityPane taskId="task-1" status="completed" />);

            const tree = await screen.findByTestId('activity-subagent-tree');
            expect(tree).toHaveTextContent('1.0-abcd1234');
            const sourceLink = screen.getByText('https://example.com/a');
            expect(sourceLink).toHaveAttribute('href', 'https://example.com/a');
            // The failure leaf shows its reason.
            expect(tree).toHaveTextContent('tool transport error');
            // Non-sub-agent rows (user + assistant turns) still render as flat rows.
            expect(screen.getByTestId('activity-row-0')).toHaveAttribute('data-kind', 'turn.user');
            expect(screen.getByText('Research the topic')).toBeInTheDocument();
            expect(screen.getByText('Here is the report.')).toBeInTheDocument();
        });

        it('tolerates an unknown marker kind (forward-compat)', async () => {
            listActivityMock.mockResolvedValue({
                events: [
                    event({ kind: 'turn.user', role: 'user', content: 'hi' }),
                    event({
                        kind: 'marker.subagent.brand_new_kind',
                        timestamp: '2026-06-08T00:00:01+00:00',
                        details: { foo: 'bar' },
                    }),
                ],
                next_cursor: null,
            });
            renderWithClient(<ActivityPane taskId="task-1" status="completed" />);
            // Renders without crashing; the unknown marker falls through to the
            // existing default renderer (system-note), not the tree.
            expect(await screen.findByText('hi')).toBeInTheDocument();
            expect(screen.queryByTestId('activity-subagent-tree')).not.toBeInTheDocument();
        });
    });
});
