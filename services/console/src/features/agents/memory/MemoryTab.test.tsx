import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { MemoryTab } from './MemoryTab';
import type {
    AgentResponse,
    MemoryEntrySummary,
    MemoryListResponse,
    MemorySearchResponse,
} from '@/types';

// -- module mocks ----------------------------------------------------------

const listAgentMemoryMock = vi.fn();
const searchAgentMemoryMock = vi.fn();
const getAgentMemoryEntryMock = vi.fn();
const deleteAgentMemoryEntryMock = vi.fn();

vi.mock('@/api/client', () => ({
    api: {
        listAgentMemory: (...args: unknown[]) => listAgentMemoryMock(...args),
        searchAgentMemory: (...args: unknown[]) => searchAgentMemoryMock(...args),
        getAgentMemoryEntry: (...args: unknown[]) => getAgentMemoryEntryMock(...args),
        deleteAgentMemoryEntry: (...args: unknown[]) => deleteAgentMemoryEntryMock(...args),
        getAgent: () => Promise.resolve(mockAgent),
    },
    ApiError: class ApiError extends Error {
        status: number;
        constructor(status: number, message: string) {
            super(message);
            this.status = status;
        }
    },
}));

const agentMock = vi.fn();
vi.mock('../useAgents', () => ({
    useAgent: () => agentMock(),
}));

const toastSuccessMock = vi.fn();
const toastErrorMock = vi.fn();
const toastInfoMock = vi.fn();
vi.mock('sonner', () => ({
    toast: {
        success: (...args: unknown[]) => toastSuccessMock(...args),
        error: (...args: unknown[]) => toastErrorMock(...args),
        info: (...args: unknown[]) => toastInfoMock(...args),
    },
}));

// -- fixtures --------------------------------------------------------------

const MOCK_AGENT_ID = 'research-agent';

let mockAgent: AgentResponse = {
    agent_id: MOCK_AGENT_ID,
    display_name: 'Research Agent',
    agent_config: {
        system_prompt: 'You are a research assistant.',
        provider: 'anthropic',
        model: 'claude-3-5-sonnet-latest',
        temperature: 0.7,
        allowed_tools: [],
        memory: { enabled: true, max_entries: 10_000 },
    },
    status: 'active',
    max_concurrent_tasks: 5,
    budget_max_per_task: 500_000,
    budget_max_per_hour: 5_000_000,
    created_at: '2026-03-27T18:00:00Z',
    updated_at: '2026-03-27T18:00:00Z',
};

function makeEntry(overrides: Partial<MemoryEntrySummary> = {}): MemoryEntrySummary {
    return {
        memory_id: overrides.memory_id ?? 'mem-' + Math.random().toString(36).slice(2, 10),
        title: overrides.title ?? 'Completed: refactor the scheduler',
        outcome: overrides.outcome ?? 'succeeded',
        task_id: overrides.task_id ?? '11111111-2222-3333-4444-555555555555',
        created_at: overrides.created_at ?? '2026-04-01T12:00:00Z',
        summary_preview: overrides.summary_preview,
        score: overrides.score,
    };
}

function renderTab(initialEntries: string[] = [`/agents/${MOCK_AGENT_ID}/memory`]) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
        <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={initialEntries}>
                <Routes>
                    <Route path="/agents/:agentId/memory" element={<MemoryTab />} />
                    <Route path="/agents/:agentId/memory/:memoryId" element={<MemoryTab />} />
                </Routes>
            </MemoryRouter>
        </QueryClientProvider>
    );
}

function setListResponse(response: Partial<MemoryListResponse> = {}) {
    (listAgentMemoryMock as Mock).mockResolvedValue({
        items: response.items ?? [],
        next_cursor: response.next_cursor,
        agent_storage_stats: response.agent_storage_stats,
    });
}

function setSearchResponse(response: Partial<MemorySearchResponse> = {}) {
    (searchAgentMemoryMock as Mock).mockResolvedValue({
        results: response.results ?? [],
        ranking_used: response.ranking_used ?? 'hybrid',
    });
}

// -- tests -----------------------------------------------------------------

beforeEach(() => {
    listAgentMemoryMock.mockReset();
    searchAgentMemoryMock.mockReset();
    getAgentMemoryEntryMock.mockReset();
    deleteAgentMemoryEntryMock.mockReset();
    toastSuccessMock.mockReset();
    toastErrorMock.mockReset();
    toastInfoMock.mockReset();
    agentMock.mockReset();

    mockAgent = {
        ...mockAgent,
        agent_config: {
            ...mockAgent.agent_config,
            memory: { enabled: true, max_entries: 10_000 },
        },
    };
    agentMock.mockReturnValue({ data: mockAgent, isLoading: false, error: null });
});

afterEach(() => {
    cleanup();
});

describe('MemoryTab — list view', () => {
    it('renders memory entries from the list endpoint', async () => {
        setListResponse({
            items: [
                makeEntry({ title: 'Completed: alpha task', memory_id: 'mem-a' }),
                makeEntry({ title: 'Completed: beta task', memory_id: 'mem-b', outcome: 'failed' }),
            ],
            agent_storage_stats: { entry_count: 2, approx_bytes: 1024 },
        });

        renderTab();

        expect(await screen.findByText('Completed: alpha task')).toBeInTheDocument();
        expect(screen.getByText('Completed: beta task')).toBeInTheDocument();
        const statsStrip = screen.getByTestId('memory-storage-stats');
        expect(within(statsStrip).getByTestId('memory-stats-entry-count').textContent).toBe('2');
        expect(within(statsStrip).getByTestId('memory-stats-max-entries').textContent).toBe('10,000');
    });

    it('shows the 80%-of-cap warning banner when over threshold', async () => {
        setListResponse({
            items: [makeEntry()],
            agent_storage_stats: { entry_count: 8500, approx_bytes: 50_000_000 },
        });

        renderTab();

        expect(await screen.findByTestId('memory-warning-banner')).toBeInTheDocument();
        expect(screen.getByTestId('memory-delete-old-button')).toBeInTheDocument();
    });

    it('renders an empty state message when the agent has no entries', async () => {
        setListResponse({
            items: [],
            agent_storage_stats: { entry_count: 0, approx_bytes: 0 },
        });

        renderTab();

        expect(await screen.findByTestId('memory-empty-state')).toBeInTheDocument();
    });

    it('shows the memory-disabled notice when agent.memory.enabled is false', async () => {
        mockAgent = {
            ...mockAgent,
            agent_config: { ...mockAgent.agent_config, memory: { enabled: false } },
        };
        agentMock.mockReturnValue({ data: mockAgent, isLoading: false, error: null });
        setListResponse({ items: [] });

        renderTab();

        expect(await screen.findByTestId('memory-disabled-notice')).toBeInTheDocument();
    });

    it('changes the outcome filter and triggers a new list query', async () => {
        setListResponse({ items: [makeEntry({ title: 'alpha' })] });

        renderTab();
        expect(await screen.findByText('alpha')).toBeInTheDocument();

        const count = listAgentMemoryMock.mock.calls.length;

        fireEvent.change(screen.getByTestId('memory-outcome-select'), { target: { value: 'failed' } });

        await waitFor(() => {
            expect(listAgentMemoryMock.mock.calls.length).toBeGreaterThan(count);
        });
        const lastCallArgs = listAgentMemoryMock.mock.calls.at(-1);
        expect(lastCallArgs?.[0]).toBe(MOCK_AGENT_ID);
        expect(lastCallArgs?.[1]).toMatchObject({ outcome: 'failed' });
    });
});

describe('MemoryTab — search mode', () => {
    it('switches to search endpoint when a query is submitted', async () => {
        setListResponse({ items: [makeEntry({ title: 'list entry' })] });
        setSearchResponse({
            results: [makeEntry({ title: 'search hit', score: 0.91 })],
            ranking_used: 'hybrid',
        });

        renderTab();
        expect(await screen.findByText('list entry')).toBeInTheDocument();

        fireEvent.change(screen.getByTestId('memory-search-input'), { target: { value: 'scheduler' } });
        fireEvent.click(screen.getByTestId('memory-search-submit'));

        expect(await screen.findByText('search hit')).toBeInTheDocument();
        expect(screen.getByTestId('memory-search-label')).toHaveTextContent(/top 20 matches/i);
        expect(searchAgentMemoryMock).toHaveBeenCalledWith(
            MOCK_AGENT_ID,
            'scheduler',
            expect.objectContaining({ mode: 'hybrid', limit: 20 })
        );
    });

    it('clears search and returns to list view', async () => {
        setListResponse({ items: [makeEntry({ title: 'list entry' })] });
        setSearchResponse({ results: [makeEntry({ title: 'search hit' })] });

        renderTab();
        expect(await screen.findByText('list entry')).toBeInTheDocument();

        fireEvent.change(screen.getByTestId('memory-search-input'), { target: { value: 'q' } });
        fireEvent.click(screen.getByTestId('memory-search-submit'));

        expect(await screen.findByText('search hit')).toBeInTheDocument();

        fireEvent.click(screen.getByTestId('memory-clear-filters'));

        expect(await screen.findByText('list entry')).toBeInTheDocument();
    });
});

describe('MemoryTab — delete flow', () => {
    it('opens the confirm dialog from the row button and calls delete on confirm', async () => {
        const entry = makeEntry({ title: 'to-delete', memory_id: 'mem-delete' });
        setListResponse({ items: [entry] });
        deleteAgentMemoryEntryMock.mockResolvedValue(undefined);

        renderTab();

        expect(await screen.findByText('to-delete')).toBeInTheDocument();

        fireEvent.click(screen.getByTestId('memory-row-delete-button'));

        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).getByText('Delete Memory Entry')).toBeInTheDocument();
        expect(within(dialog).getByText('to-delete')).toBeInTheDocument();

        fireEvent.click(within(dialog).getByRole('button', { name: /delete/i }));

        await waitFor(() => {
            expect(deleteAgentMemoryEntryMock).toHaveBeenCalledWith(MOCK_AGENT_ID, 'mem-delete');
        });
        await waitFor(() => {
            expect(toastSuccessMock).toHaveBeenCalledWith('Memory entry deleted');
        });
    });
});

describe('MemoryTab — detail view routing', () => {
    it('renders the detail component when the URL points to a memory entry', async () => {
        setListResponse({ items: [] });
        getAgentMemoryEntryMock.mockResolvedValue({
            memory_id: 'mem-detail-1',
            agent_id: MOCK_AGENT_ID,
            task_id: 'task-abc',
            title: 'Detail Title',
            summary: 'Detail summary body',
            observations: ['observation one', 'observation two'],
            outcome: 'succeeded',
            tags: ['tag1'],
            summarizer_model_id: 'claude-haiku',
            version: 1,
            created_at: '2026-04-01T12:00:00Z',
            updated_at: '2026-04-01T12:00:00Z',
        });

        renderTab([`/agents/${MOCK_AGENT_ID}/memory/mem-detail-1`]);

        expect(await screen.findByText('Detail Title')).toBeInTheDocument();
        expect(screen.getByText('Detail summary body')).toBeInTheDocument();
        expect(screen.getByText('observation one')).toBeInTheDocument();
        expect(screen.getByText('observation two')).toBeInTheDocument();
    });
});
