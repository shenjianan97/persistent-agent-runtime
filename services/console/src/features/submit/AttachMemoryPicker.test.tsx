import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AttachMemoryPicker, MAX_ATTACHED_MEMORIES } from './AttachMemoryPicker';
import type { MemoryEntrySummary } from '@/types';

const listMock = vi.fn();
const searchMock = vi.fn();

vi.mock('@/api/client', () => ({
    api: {
        listAgentMemory: (...args: unknown[]) => listMock(...args),
        searchAgentMemory: (...args: unknown[]) => searchMock(...args),
        getAgentMemoryEntry: vi.fn(),
        deleteAgentMemoryEntry: vi.fn(),
    },
}));

function sample(idx: number, overrides?: Partial<MemoryEntrySummary>): MemoryEntrySummary {
    return {
        memory_id: `mem-${idx}`,
        title: `Entry ${idx}`,
        outcome: 'succeeded',
        task_id: `task-${idx}`,
        created_at: '2026-04-17T00:00:00Z',
        summary_preview: `Preview ${idx}`,
        ...overrides,
    };
}

function createWrapper() {
    const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
    });
    return ({ children }: { children: React.ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
}

beforeEach(() => {
    listMock.mockReset();
    searchMock.mockReset();
    listMock.mockResolvedValue({ items: [sample(1), sample(2), sample(3)], next_cursor: null });
    searchMock.mockResolvedValue({ results: [sample(1)], ranking_used: 'hybrid' });
});

afterEach(cleanup);

describe('AttachMemoryPicker', () => {
    it('does not hit the list endpoint until the picker is opened', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker agentId="agent-1" value={[]} onChange={onChange} />,
            { wrapper: createWrapper() }
        );
        // Wait a tick to ensure the query resolver has had a chance to run.
        await new Promise((resolve) => setTimeout(resolve, 10));
        expect(listMock).not.toHaveBeenCalled();
    });

    it('lists entries after the panel is opened', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker agentId="agent-1" value={[]} onChange={onChange} />,
            { wrapper: createWrapper() }
        );

        fireEvent.click(screen.getByRole('button', { name: /browse/i }));

        await waitFor(() => expect(listMock).toHaveBeenCalled());
        expect(await screen.findByText('Entry 1')).toBeInTheDocument();
        expect(screen.getByText('Entry 2')).toBeInTheDocument();
    });

    it('switches to the search endpoint when the query is non-empty', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker agentId="agent-1" value={[]} onChange={onChange} />,
            { wrapper: createWrapper() }
        );

        fireEvent.click(screen.getByRole('button', { name: /browse/i }));
        const searchInput = await screen.findByLabelText(/search memory entries/i);
        fireEvent.change(searchInput, { target: { value: 'foo' } });

        await waitFor(() => expect(searchMock).toHaveBeenCalled());
        const lastCallArgs = searchMock.mock.calls[searchMock.mock.calls.length - 1];
        expect(lastCallArgs[0]).toBe('agent-1');
        expect(lastCallArgs[1]).toBe('foo');
    });

    it('toggles selection and calls onChange with the new list', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker agentId="agent-1" value={[]} onChange={onChange} />,
            { wrapper: createWrapper() }
        );

        fireEvent.click(screen.getByRole('button', { name: /browse/i }));
        const firstItem = await screen.findByRole('option', { name: /Entry 1/i });
        fireEvent.click(firstItem);

        expect(onChange).toHaveBeenCalledWith(['mem-1']);
    });

    it('enforces the selection cap', async () => {
        const onChange = vi.fn();
        const preSelected = Array.from({ length: MAX_ATTACHED_MEMORIES }, (_, i) => `prefill-${i}`);
        render(
            <AttachMemoryPicker agentId="agent-1" value={preSelected} onChange={onChange} />,
            { wrapper: createWrapper() }
        );

        fireEvent.click(screen.getByRole('button', { name: /browse/i }));
        const firstItem = await screen.findByRole('option', { name: /Entry 1/i });
        fireEvent.click(firstItem);
        // Already at cap — onChange should NOT fire for a net-new selection.
        expect(onChange).not.toHaveBeenCalled();
        // Sanity: the warning banner is rendered.
        expect(screen.getByText(/Selection capped at/i)).toBeInTheDocument();
    });

    it('renders the Selected panel with remove buttons when value is non-empty', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker
                agentId="agent-1"
                value={['mem-1']}
                onChange={onChange}
                selectedSummaries={{ 'mem-1': sample(1) }}
            />,
            { wrapper: createWrapper() }
        );

        expect(screen.getByTestId('attach-memory-selected-panel')).toBeInTheDocument();
        const removeBtn = screen.getByRole('button', { name: /remove Entry 1/i });
        fireEvent.click(removeBtn);
        expect(onChange).toHaveBeenCalledWith([]);
    });

    it('clears the search term on Escape', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker agentId="agent-1" value={[]} onChange={onChange} />,
            { wrapper: createWrapper() }
        );

        fireEvent.click(screen.getByRole('button', { name: /browse/i }));
        const searchInput = await screen.findByLabelText(/search memory entries/i);
        fireEvent.change(searchInput, { target: { value: 'anything' } });
        expect((searchInput as HTMLInputElement).value).toBe('anything');
        await act(async () => {
            fireEvent.keyDown(searchInput, { key: 'Escape' });
        });
        expect((searchInput as HTMLInputElement).value).toBe('');
    });

    it('selects the focused item when Enter is pressed', async () => {
        const onChange = vi.fn();
        render(
            <AttachMemoryPicker agentId="agent-1" value={[]} onChange={onChange} />,
            { wrapper: createWrapper() }
        );

        fireEvent.click(screen.getByRole('button', { name: /browse/i }));
        const searchInput = await screen.findByLabelText(/search memory entries/i);
        await screen.findByRole('option', { name: /Entry 1/i });
        fireEvent.keyDown(searchInput, { key: 'ArrowDown' });
        fireEvent.keyDown(searchInput, { key: 'Enter' });
        expect(onChange).toHaveBeenCalled();
        const [lastArg] = onChange.mock.calls[onChange.mock.calls.length - 1];
        expect(Array.isArray(lastArg)).toBe(true);
        expect(lastArg.length).toBe(1);
    });
});
