import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SubmitTaskPage } from './SubmitTaskPage';

// vi.hoisted is required so these mutable fixtures are initialised *before*
// the vi.mock factories below run. Without it the factories reference a
// temporal-dead-zone `const` and Vitest fails at module-load time.
const mocks = vi.hoisted(() => ({
    navigateMock: vi.fn(),
    mutateMock: vi.fn(),
    toastInfoMock: vi.fn(),
    toastErrorMock: vi.fn(),
    toastSuccessMock: vi.fn(),
    searchParamsInit: new URLSearchParams(),
    agentFixture: null as any,
    agentIsLoading: false as boolean,
    agentsListFixture: [] as any[],
    memoryListResult: {
        data: { items: [], next_cursor: null },
        isFetching: false,
        error: null,
        isError: false,
    } as any,
    memoryDetailResult: {
        data: undefined,
        isFetching: false,
        isError: false,
        error: null,
    } as any,
    memorySearchResult: {
        data: undefined,
        isFetching: false,
        error: null,
        isError: false,
    } as any,
}));

vi.mock('react-router', async () => {
    const actual = await vi.importActual<typeof import('react-router')>('react-router');
    return {
        ...actual,
        useNavigate: () => mocks.navigateMock,
        useSearchParams: () => [mocks.searchParamsInit, vi.fn()],
    };
});

vi.mock('./useSubmitTask', () => ({
    useSubmitTask: () => ({
        mutate: mocks.mutateMock,
        isPending: false,
    }),
}));

vi.mock('@/features/agents/useAgents', () => ({
    useAgents: () => ({
        data: mocks.agentsListFixture,
        isLoading: false,
    }),
    useAgent: () => ({
        data: mocks.agentIsLoading ? undefined : mocks.agentFixture,
        isLoading: mocks.agentIsLoading,
    }),
}));

vi.mock('@/features/settings/useLangfuseEndpoints', () => ({
    useLangfuseEndpoints: () => ({ data: [] }),
}));

vi.mock('@/features/agents/memory/hooks', () => ({
    useAgentMemoryList: () => mocks.memoryListResult,
    useAgentMemorySearch: () => mocks.memorySearchResult,
    useAgentMemoryDetail: () => mocks.memoryDetailResult,
    useDeleteAgentMemoryEntry: () => ({ mutate: vi.fn() }),
}));

vi.mock('sonner', () => ({
    toast: {
        success: mocks.toastSuccessMock,
        error: mocks.toastErrorMock,
        info: mocks.toastInfoMock,
    },
}));

function createWrapper() {
    const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
    });
    return ({ children }: { children: React.ReactNode }) => (
        <QueryClientProvider client={queryClient}>
            <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
    );
}

beforeEach(() => {
    mocks.searchParamsInit = new URLSearchParams();
    mocks.agentIsLoading = false;
    mocks.agentsListFixture = [
        {
            agent_id: 'test-agent',
            display_name: 'Test Agent',
            provider: 'anthropic',
            model: 'claude-3-5-sonnet-latest',
            status: 'active',
            created_at: '',
            updated_at: '',
        },
    ];
    mocks.agentFixture = {
        agent_id: 'test-agent',
        display_name: 'Test Agent',
        agent_config: {
            system_prompt: 'You are helpful.',
            provider: 'anthropic',
            model: 'claude-3-5-sonnet-latest',
            temperature: 0.7,
            allowed_tools: ['web_search'],
        },
        status: 'active',
        created_at: '',
        updated_at: '',
    };
    mocks.memoryListResult.data = { items: [], next_cursor: null };
    mocks.memoryDetailResult.data = undefined;
    mocks.memoryDetailResult.isError = false;
    mocks.memoryDetailResult.isFetching = false;
    mocks.mutateMock.mockReset();
    mocks.navigateMock.mockReset();
    mocks.toastInfoMock.mockReset();
    mocks.toastErrorMock.mockReset();
    mocks.toastSuccessMock.mockReset();
});

afterEach(() => {
    cleanup();
});

describe('SubmitTaskPage', () => {
    it('renders submit page with agent selector', () => {
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        expect(screen.getByRole('heading', { name: 'Submit Task' })).toBeInTheDocument();
        expect(screen.getByLabelText('Agent')).toBeInTheDocument();
    });

    it('shows agent options in the dropdown', () => {
        const { container } = render(<SubmitTaskPage />, { wrapper: createWrapper() });
        const agentSelect = screen.getByLabelText('Agent');
        expect(agentSelect).toBeInTheDocument();
        const options = Array.from(container.querySelectorAll('option')).map(o => o.textContent);
        expect(options).toContain('Test Agent (test-agent)');
    });
});

describe('SubmitTaskPage empty state', () => {
    it('shows empty state when no agents exist', () => {
        mocks.agentsListFixture = [];
        mocks.agentFixture = null;
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        expect(screen.getByRole('heading', { name: 'Submit Task' })).toBeInTheDocument();
    });
});

describe('SubmitTaskPage — memory disabled agent', () => {
    it('hides the memory attach picker when memory.enabled is false', async () => {
        mocks.agentFixture.agent_config.memory = { enabled: false };
        mocks.searchParamsInit = new URLSearchParams({ agent_id: 'test-agent' });
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        // The memory card now always renders when an agent is selected so the
        // mode dropdown can show in its disabled state.
        expect(await screen.findByTestId('memory-attach-card')).toBeInTheDocument();
        // But the attach picker itself is gone.
        expect(screen.queryByTestId('attach-memory-picker')).not.toBeInTheDocument();
    });

    it('shows the memory-mode select disabled + locked to skip when memory is disabled', async () => {
        mocks.agentFixture.agent_config.memory = { enabled: false };
        mocks.searchParamsInit = new URLSearchParams({ agent_id: 'test-agent' });
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        const trigger = await screen.findByTestId('memory-mode-select');
        expect(trigger).toBeInTheDocument();
        expect(trigger).toBeDisabled();
        // The displayed value snaps to "Don't save memory".
        expect(trigger).toHaveTextContent(/don'?t save memory/i);
        expect(screen.getByText(/this agent has memory disabled/i)).toBeInTheDocument();
    });

    it('forces memory_mode=skip in the payload when memory is disabled', async () => {
        mocks.agentFixture.agent_config.memory = { enabled: false };
        mocks.searchParamsInit = new URLSearchParams({ agent_id: 'test-agent' });
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        const input = screen.getByLabelText(/Input Directive/i);
        fireEvent.change(input, { target: { value: 'hello' } });
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: /submit task/i }));
        });
        await waitFor(() => expect(mocks.mutateMock).toHaveBeenCalled());
        const [{ request }] = mocks.mutateMock.mock.calls[mocks.mutateMock.mock.calls.length - 1];
        expect(request.memory_mode).toBe('skip');
    });
});

describe('SubmitTaskPage — memory enabled agent', () => {
    beforeEach(() => {
        mocks.agentFixture.agent_config.memory = { enabled: true };
        mocks.searchParamsInit = new URLSearchParams({ agent_id: 'test-agent' });
    });

    it('renders the memory card with the memory-mode select defaulting to always', async () => {
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        expect(await screen.findByTestId('memory-attach-card')).toBeInTheDocument();
        const trigger = screen.getByTestId('memory-mode-select');
        expect(trigger).toBeInTheDocument();
        expect(trigger).not.toBeDisabled();
        expect(trigger).toHaveTextContent(/always save memory/i);
    });

    it('includes memory_mode=always in the payload by default', async () => {
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        const input = screen.getByLabelText(/Input Directive/i);
        fireEvent.change(input, { target: { value: 'hello' } });

        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: /submit task/i }));
        });
        await waitFor(() => expect(mocks.mutateMock).toHaveBeenCalled());
        const [{ request }] = mocks.mutateMock.mock.calls[mocks.mutateMock.mock.calls.length - 1];
        expect(request.memory_mode).toBe('always');
        expect(request.skip_memory_write).toBeUndefined();
        expect(request.attached_memory_ids).toBeUndefined();
    });
});

describe('SubmitTaskPage — agent config still loading', () => {
    // Regression: previously ``memoryEnabled`` collapsed "unknown/loading" and
    // "explicitly disabled" into the same false state, so a fast submit while
    // useAgent was still fetching would silently force ``memory_mode=skip`` on
    // what turned out to be a memory-enabled agent. Guard: the submit button
    // must be disabled while ``useAgent`` is in flight, and if the coercion
    // path ever runs it must NOT force ``skip`` without a resolved agent.

    it('disables the submit button while useAgent is loading', async () => {
        mocks.agentIsLoading = true;
        mocks.searchParamsInit = new URLSearchParams({ agent_id: 'test-agent' });
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        const btn = await screen.findByRole('button', { name: /loading agent/i });
        expect(btn).toBeDisabled();
    });

    it('does NOT coerce memory_mode=skip just because agent config is still loading', async () => {
        // First render: loading → user tries to submit early. The button is
        // disabled so mutate shouldn't fire, but we also assert the coercion
        // path itself would not set 'skip'. To exercise that belt-and-
        // suspenders path, flip to "resolved + memory ENABLED" and submit;
        // the payload must be 'always', never 'skip'.
        mocks.agentFixture.agent_config.memory = { enabled: true };
        mocks.searchParamsInit = new URLSearchParams({ agent_id: 'test-agent' });
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        const input = screen.getByLabelText(/Input Directive/i);
        fireEvent.change(input, { target: { value: 'hello' } });
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: /submit task/i }));
        });
        await waitFor(() => expect(mocks.mutateMock).toHaveBeenCalled());
        const [{ request }] = mocks.mutateMock.mock.calls[mocks.mutateMock.mock.calls.length - 1];
        expect(request.memory_mode).toBe('always');
    });
});

describe('SubmitTaskPage — deep-link pre-selection', () => {
    it('toasts and skips selection when the deep-linked agent has memory disabled', async () => {
        mocks.agentFixture.agent_config.memory = { enabled: false };
        mocks.searchParamsInit = new URLSearchParams({
            agent_id: 'test-agent',
            attachMemoryId: 'mem-42',
        });
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        await waitFor(() =>
            expect(mocks.toastInfoMock).toHaveBeenCalledWith(
                expect.stringMatching(/memory is disabled/i)
            )
        );
        // Attach picker must NOT render for a memory-disabled agent even though
        // the mode-select card now shows unconditionally.
        expect(screen.queryByTestId('attach-memory-picker')).not.toBeInTheDocument();
    });

    it('pre-selects the entry when the agent has memory enabled and detail resolves', async () => {
        mocks.agentFixture.agent_config.memory = { enabled: true };
        mocks.searchParamsInit = new URLSearchParams({
            agent_id: 'test-agent',
            attachMemoryId: 'mem-42',
        });
        mocks.memoryDetailResult.data = {
            memory_id: 'mem-42',
            agent_id: 'test-agent',
            task_id: 'task-42',
            title: 'Deep-linked entry',
            summary: 'The deep-linked memory entry summary.',
            observations: [],
            outcome: 'succeeded',
            tags: [],
            version: 1,
            created_at: '2026-04-17T00:00:00Z',
            updated_at: '2026-04-17T00:00:00Z',
        };
        render(<SubmitTaskPage />, { wrapper: createWrapper() });
        expect(await screen.findByText('Deep-linked entry')).toBeInTheDocument();
    });
});
