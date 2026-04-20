import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConversationPane } from '../ConversationPane';
import type { ConversationEntry, ConversationListResponse } from '@/types';

// ─── Mock the API client ───────────────────────────────────────────

const listConversationMock = vi.fn();

vi.mock('@/api/client', () => ({
    api: {
        listConversation: (...args: unknown[]) => listConversationMock(...args),
    },
    ApiError: class ApiError extends Error {
        status: number;
        constructor(status: number, message: string) {
            super(message);
            this.status = status;
        }
    },
}));

// ─── Fixtures ──────────────────────────────────────────────────────

function entry(partial: Partial<ConversationEntry> & Pick<ConversationEntry, 'sequence' | 'kind'>): ConversationEntry {
    return {
        content_version: 1,
        content: {},
        metadata: {},
        created_at: '2026-04-19T10:00:00Z',
        ...partial,
    } as ConversationEntry;
}

const ALL_KINDS_RESPONSE: ConversationListResponse = {
    entries: [
        entry({ sequence: 1, kind: 'user_turn', content: { text: 'Hello agent' } }),
        entry({ sequence: 2, kind: 'agent_turn', content: { text: 'Hi human!' } }),
        entry({
            sequence: 3,
            kind: 'tool_call',
            content: { tool_name: 'read_url', args: { url: 'https://example.com' } },
        }),
        entry({
            sequence: 4,
            kind: 'tool_result',
            content: { tool_name: 'read_url', output: 'Example Domain' },
            metadata: { capped: true, orig_bytes: 123456 },
        }),
        entry({
            sequence: 5,
            kind: 'compaction_boundary',
            content: { summary_text: 'Summarised older turns.' },
            metadata: {
                first_turn_index: 3,
                last_turn_index: 12,
                turns_summarized: 10,
                summarizer_model: 'claude-haiku-4-5',
                summary_bytes: 420,
                cost_microdollars: 700,
                tier3_firing_index: 1,
            },
        }),
        entry({ sequence: 6, kind: 'memory_flush', content: {} }),
        entry({
            sequence: 7,
            kind: 'hitl_pause',
            content: {
                reason: 'awaiting approval',
                prompt_to_user: 'Should I proceed with the destructive action?',
            },
        }),
        entry({
            sequence: 8,
            kind: 'hitl_resume',
            content: {
                resolution: 'approved',
                user_note: 'Go ahead.',
            },
        }),
        entry({ sequence: 9, kind: 'system_note', content: { text: 'Task was retried once.' } }),
        entry({
            sequence: 10,
            kind: 'offload_emitted',
            content: { count: 3, total_bytes: 43_008, step_index: 7 },
        }),
    ],
};

// ─── Rendering helpers ─────────────────────────────────────────────

function createClient() {
    return new QueryClient({
        defaultOptions: {
            queries: { retry: false, staleTime: 0, gcTime: 0 },
            mutations: { retry: false },
        },
    });
}

async function renderPane(
    props: { taskId?: string; status?: Parameters<typeof ConversationPane>[0]['status'] } = {},
) {
    const client = createClient();
    const utils = render(
        <QueryClientProvider client={client}>
            <ConversationPane
                taskId={props.taskId ?? 'task-1'}
                status={props.status ?? 'running'}
            />
        </QueryClientProvider>,
    );
    // Wait for the initial query to resolve (or fail gracefully).
    await waitFor(() => {
        expect(listConversationMock).toHaveBeenCalled();
        expect(listConversationMock.mock.calls[0][0]).toBe('task-1');
    });
    return { ...utils, client };
}

// ─── Reset ─────────────────────────────────────────────────────────

beforeEach(() => {
    listConversationMock.mockReset();
});

afterEach(() => {
    cleanup();
    vi.useRealTimers();
});

// ─── Tests ─────────────────────────────────────────────────────────

describe('ConversationPane — rendering', () => {
    it('renders all known kinds with kind-specific testids', async () => {
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);

        await renderPane({ status: 'completed' });

        await waitFor(() => {
            expect(screen.getByTestId('conversation-entry-user_turn')).toBeInTheDocument();
        });
        expect(screen.getByTestId('conversation-entry-agent_turn')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-tool_call')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-tool_result')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-compaction_boundary')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-memory_flush')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-hitl_pause')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-hitl_resume')).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-system_note')).toBeInTheDocument();
        expect(
            screen.getByTestId('conversation-entry-offload_emitted'),
        ).toBeInTheDocument();
    });

    it('renders the offload_emitted banner with count and byte roll-up', async () => {
        listConversationMock.mockResolvedValue({
            entries: [
                entry({
                    sequence: 1,
                    kind: 'offload_emitted',
                    content: { count: 3, total_bytes: 43_008, step_index: 7 },
                }),
            ],
        });
        await renderPane({ status: 'completed' });

        const banner = await screen.findByTestId(
            'conversation-entry-offload_emitted',
        );
        // Count + human-readable byte roll-up (42.0 KB for 43008 bytes).
        expect(banner).toHaveTextContent(/3 older tool outputs archived/);
        expect(banner).toHaveTextContent(/42\.0 KB/);
    });

    it('uses singular copy when a single item was offloaded', async () => {
        listConversationMock.mockResolvedValue({
            entries: [
                entry({
                    sequence: 1,
                    kind: 'offload_emitted',
                    content: { count: 1, total_bytes: 20_480, step_index: 2 },
                }),
            ],
        });
        await renderPane({ status: 'completed' });
        const banner = await screen.findByTestId(
            'conversation-entry-offload_emitted',
        );
        expect(banner).toHaveTextContent(/1 older tool output archived/);
        expect(banner).not.toHaveTextContent(/tool outputs/);
    });

    it('renders user / agent turn text content', async () => {
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);
        await renderPane({ status: 'completed' });
        await waitFor(() => {
            expect(screen.getByText('Hello agent')).toBeInTheDocument();
        });
        expect(screen.getByText('Hi human!')).toBeInTheDocument();
    });

    it('renders capped tool_result with the explicit copy including the original byte count', async () => {
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);
        await renderPane({ status: 'completed' });

        // Expand the tool_result fold so the capped notice becomes visible.
        const toolResultRow = await screen.findByTestId('conversation-entry-tool_result');
        const expandButton = toolResultRow.querySelector('button');
        expect(expandButton).toBeTruthy();
        fireEvent.click(expandButton!);

        const notice = await screen.findByTestId('conversation-tool-result-capped-notice');
        expect(notice).toHaveTextContent('Tool returned 123456 bytes');
        expect(notice).toHaveTextContent('head+tail capped at 25KB');
        expect(notice).toHaveTextContent('same view the model had');
    });

    it('renders HITL pause and resume banners with spec copy', async () => {
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);
        await renderPane({ status: 'completed' });

        const pause = await screen.findByTestId('conversation-entry-hitl_pause');
        expect(pause).toHaveTextContent('Paused awaiting human approval: awaiting approval');
        expect(pause).toHaveTextContent('Should I proceed with the destructive action?');

        const resume = screen.getByTestId('conversation-entry-hitl_resume');
        expect(resume).toHaveTextContent('Resumed: approved');
        expect(resume).toHaveTextContent('Go ahead.');
    });
});

describe('ConversationPane — compaction boundary', () => {
    it('renders the divider with turn indices and expands to show summary + operator fold', async () => {
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);
        await renderPane({ status: 'completed' });

        const divider = await screen.findByTestId('conversation-compaction-divider');
        expect(divider).toHaveTextContent('Context summarized (turns 3–12, 10 turns)');

        // Collapsed: summary text is hidden.
        expect(screen.queryByText('Summarised older turns.')).toBeNull();

        fireEvent.click(divider);

        await waitFor(() => {
            expect(screen.getByText('Summarised older turns.')).toBeInTheDocument();
        });

        // Operator fold is present but starts collapsed.
        const operatorFold = screen.getByTestId('conversation-operator-fold');
        const operatorFoldButton = operatorFold.querySelector('button');
        expect(operatorFoldButton).toBeTruthy();
        fireEvent.click(operatorFoldButton!);

        expect(screen.getByText('claude-haiku-4-5')).toBeInTheDocument();
        expect(screen.getByText('420')).toBeInTheDocument();
        expect(screen.getByText('700')).toBeInTheDocument();
    });
});

describe('ConversationPane — unknown kind / future content_version', () => {
    it('renders a neutral debug-fold banner for content_version > 1', async () => {
        listConversationMock.mockResolvedValue({
            entries: [
                entry({
                    sequence: 1,
                    kind: 'user_turn',
                    content_version: 2,
                    content: { text: 'future payload' },
                }),
            ],
        });
        await renderPane({ status: 'completed' });
        const banner = await screen.findByTestId('conversation-entry-unknown');
        expect(banner).toHaveTextContent('System event');
        // Expand the debug fold and assert raw JSON is visible.
        const foldButton = banner.querySelector('button');
        fireEvent.click(foldButton!);
        await waitFor(() => {
            expect(banner.textContent).toContain('future payload');
        });
    });

    it('renders a debug-fold banner for unknown kinds', async () => {
        listConversationMock.mockResolvedValue({
            entries: [
                entry({
                    sequence: 1,
                    kind: 'brand_new_kind',
                    content: { foo: 'bar' },
                }),
            ],
        });
        await renderPane({ status: 'completed' });
        const banner = await screen.findByTestId('conversation-entry-unknown');
        expect(banner).toHaveTextContent('brand_new_kind');
    });
});

describe('ConversationPane — polling', () => {
    it('does not refetch once the task is in a terminal state', async () => {
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);
        await renderPane({ status: 'completed' });
        await waitFor(() => {
            expect(listConversationMock).toHaveBeenCalledTimes(1);
        });
        // Wait past several polling intervals (the component would poll at
        // 5s if not terminal); no new calls expected.
        await new Promise((resolve) => setTimeout(resolve, 200));
        expect(listConversationMock).toHaveBeenCalledTimes(1);
    });

    it('refetches on a 5-second interval while non-terminal', async () => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        listConversationMock.mockResolvedValue(ALL_KINDS_RESPONSE);
        await renderPane({ status: 'running' });
        await waitFor(() => {
            expect(listConversationMock).toHaveBeenCalledTimes(1);
        });
        await act(async () => {
            await vi.advanceTimersByTimeAsync(5100);
        });
        expect(listConversationMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
});

describe('ConversationPane — "N new entries" pill', () => {
    it('renders the pill when new entries arrive while scrolled away from tail', async () => {
        const initial: ConversationListResponse = {
            entries: [
                entry({ sequence: 1, kind: 'agent_turn', content: { text: 'One' } }),
                entry({ sequence: 2, kind: 'agent_turn', content: { text: 'Two' } }),
            ],
        };
        listConversationMock.mockResolvedValueOnce(initial);

        const { client } = await renderPane({ status: 'running' });
        await waitFor(() => {
            expect(screen.getAllByTestId('conversation-entry-agent_turn')).toHaveLength(2);
        });

        // Simulate user scrolling up — the scroll container sees a non-zero gap
        // between (scrollHeight - scrollTop) and clientHeight.
        const scroller = screen.getByTestId('conversation-scroll');
        Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 1000 });
        Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 400 });
        Object.defineProperty(scroller, 'scrollTop', { configurable: true, value: 100 });
        fireEvent.scroll(scroller);

        // Now feed a longer list and force a refetch.
        const longer: ConversationListResponse = {
            entries: [
                ...initial.entries,
                entry({ sequence: 3, kind: 'agent_turn', content: { text: 'Three' } }),
                entry({ sequence: 4, kind: 'agent_turn', content: { text: 'Four' } }),
            ],
        };
        listConversationMock.mockResolvedValue(longer);
        await act(async () => {
            await client.invalidateQueries({ queryKey: ['task-conversation', 'task-1'] });
        });

        await waitFor(() => {
            expect(screen.getByTestId('conversation-new-activity-pill')).toBeInTheDocument();
        });
        expect(screen.getByTestId('conversation-new-activity-pill')).toHaveTextContent(
            /2 new entries/,
        );
    });
});

describe('ConversationPane — error boundary', () => {
    it('renders a failure banner when the conversation fetch rejects', async () => {
        listConversationMock.mockRejectedValue(new Error('network down'));
        await renderPane({ status: 'running' });
        await waitFor(() => {
            expect(screen.getByTestId('conversation-pane-error')).toBeInTheDocument();
        });
        expect(screen.getByTestId('conversation-pane-error')).toHaveTextContent('network down');
    });
});

// Regression guard: the API caps one page at 200 rows. Without pagination the
// Console silently loses everything past the cap — including the new entries a
// long-running task emits right around the time Tier 3 compaction fires.
describe('ConversationPane — server-side pagination', () => {
    it('walks next_sequence until the server stops setting it', async () => {
        const firstPage: ConversationListResponse = {
            entries: [
                entry({ sequence: 1, kind: 'agent_turn', content: { text: 'first' } }),
                entry({ sequence: 2, kind: 'agent_turn', content: { text: 'second' } }),
            ],
            next_sequence: 2,
        };
        const secondPage: ConversationListResponse = {
            entries: [
                entry({ sequence: 3, kind: 'compaction_boundary', content: { summary_text: 'mid' } }),
                entry({ sequence: 4, kind: 'agent_turn', content: { text: 'post-compaction' } }),
            ],
            next_sequence: 4,
        };
        const thirdPage: ConversationListResponse = {
            entries: [
                entry({ sequence: 5, kind: 'tool_call', content: { tool_name: 'search', args: {} } }),
            ],
            // No next_sequence → client should stop.
        };

        listConversationMock
            .mockResolvedValueOnce(firstPage)
            .mockResolvedValueOnce(secondPage)
            .mockResolvedValueOnce(thirdPage);

        await renderPane({ status: 'completed' });

        // All pages merge → everything renders, including the post-compaction row.
        await waitFor(() => {
            expect(screen.getByText('post-compaction')).toBeInTheDocument();
        });
        expect(screen.getByText('first')).toBeInTheDocument();
        expect(screen.getByText('second')).toBeInTheDocument();
        expect(
            screen.getByTestId('conversation-entry-compaction_boundary'),
        ).toBeInTheDocument();
        expect(screen.getByTestId('conversation-entry-tool_call')).toBeInTheDocument();

        // Pagination cursor contract: page 1 passes no cursor, subsequent pages
        // carry the prior page's next_sequence.
        expect(listConversationMock.mock.calls[0][1]).toBeUndefined();
        expect(listConversationMock.mock.calls[1][1]).toBe(2);
        expect(listConversationMock.mock.calls[2][1]).toBe(4);
        expect(listConversationMock).toHaveBeenCalledTimes(3);
    });

    it('stops at the first page when the server does not set next_sequence', async () => {
        listConversationMock.mockResolvedValue({
            entries: [
                entry({ sequence: 1, kind: 'agent_turn', content: { text: 'only page' } }),
            ],
        });
        await renderPane({ status: 'completed' });
        await waitFor(() => {
            expect(screen.getByText('only page')).toBeInTheDocument();
        });
        // No follow-up pagination call.
        expect(listConversationMock).toHaveBeenCalledTimes(1);
    });
});
