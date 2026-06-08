import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AgentDetailPage } from './AgentDetailPage';

const navigateMock = vi.fn();
const agentMock = vi.fn();
const updateMock = vi.fn();

vi.mock('react-router', async () => {
    const actual = await vi.importActual<typeof import('react-router')>('react-router');
    return {
        ...actual,
        useNavigate: () => navigateMock,
        useParams: () => ({ agentId: 'research-agent' }),
    };
});

vi.mock('./useAgents', () => ({
    useAgent: () => agentMock(),
    useUpdateAgent: () => ({
        mutate: updateMock,
        isPending: false,
    }),
}));

vi.mock('@/features/submit/useModels', () => ({
    useModels: () => ({
        data: [
            { provider: 'anthropic', model_id: 'claude-3-5-sonnet-latest', display_name: 'Claude 3.5 Sonnet' },
            { provider: 'openai', model_id: 'gpt-4o', display_name: 'GPT-4o' },
        ],
        isLoading: false,
    }),
}));

vi.mock('sonner', () => ({
    toast: {
        success: vi.fn(),
        error: vi.fn(),
    },
}));

vi.mock('../tool-servers/useToolServers', () => ({
    useToolServers: () => ({
        data: [],
        isLoading: false,
    }),
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

afterEach(() => {
    cleanup();
    agentMock.mockReset();
    navigateMock.mockReset();
    updateMock.mockReset();
});

const MOCK_AGENT = {
    agent_id: 'research-agent',
    display_name: 'Research Agent',
    agent_config: {
        system_prompt: 'You are a research assistant.',
        provider: 'anthropic',
        model: 'claude-3-5-sonnet-latest',
        temperature: 0.7,
        allowed_tools: ['web_search'],
    },
    status: 'active' as const,
    max_concurrent_tasks: 5,
    budget_max_per_task: 500000,
    budget_max_per_hour: 5000000,
    created_at: '2026-03-27T18:00:00Z',
    updated_at: '2026-03-27T18:00:00Z',
};

const MOCK_AGENT_WITH_SANDBOX = {
    ...MOCK_AGENT,
    agent_config: {
        ...MOCK_AGENT.agent_config,
        sandbox: {
            enabled: true,
            template: 'python-3.11',
            vcpu: 2,
            memory_mb: 2048,
            timeout_seconds: 3600,
        },
    },
};

const MOCK_AGENT_WITH_MEMORY = {
    ...MOCK_AGENT,
    agent_config: {
        ...MOCK_AGENT.agent_config,
        memory: {
            enabled: true,
            summarizer_model: 'claude-3-5-sonnet-latest',
            max_entries: 2500,
        },
    },
};

const MOCK_AGENT_WITH_MEMORY_DEFAULTS = {
    ...MOCK_AGENT,
    agent_config: {
        ...MOCK_AGENT.agent_config,
        memory: {
            enabled: true,
        },
    },
};

const MOCK_AGENT_WITH_CONTEXT_MANAGEMENT = {
    ...MOCK_AGENT,
    agent_config: {
        ...MOCK_AGENT.agent_config,
        context_management: {
            summarizer_model: 'gpt-4o-mini',
            summarizer_provider: 'openai',
            exclude_tools: ['web_search'],
            pre_tier3_memory_flush: true,
        },
    },
};

// Supervisor topology ("Deep Research") agent — exercises the S10 read-only
// preset/topology rendering + the read-only Deep Research config section.
const MOCK_AGENT_SUPERVISOR = {
    ...MOCK_AGENT,
    agent_config: {
        ...MOCK_AGENT.agent_config,
        topology: 'supervisor' as const,
        preset: 'research',
        supervisor: {
            max_fanout_per_iteration: 7,
            max_iterations: 4,
            source_allowlist: ['web_search', 'internal_docs'],
            writer_style: 'annotated_bullets' as const,
            scope_clarification_enabled: true,
        },
    },
};

describe('AgentDetailPage', () => {
    it('shows sandbox info in read-only mode when sandbox is enabled', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_SANDBOX, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(screen.getByText('python-3.11')).toBeInTheDocument();
        expect(screen.getByText('2048 MB')).toBeInTheDocument();
        expect(screen.getByText('3600s')).toBeInTheDocument();
    });

    it('does not show sandbox section in read-only mode when sandbox is disabled', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(screen.queryByText('python-3.11')).not.toBeInTheDocument();
    });

    it('shows sandbox fields in edit mode', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_SANDBOX, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /edit/i }));

        // Sandbox enable checkbox should appear
        expect(screen.getByText('Enable Sandbox')).toBeInTheDocument();
        // Sandbox conditional fields should be visible because sandbox is enabled
        expect(screen.getByDisplayValue('python-3.11')).toBeInTheDocument();
    });

    it('includes sandbox config in submit payload when sandbox is enabled', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_SANDBOX, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

        await waitFor(() => expect(updateMock).toHaveBeenCalled());

        expect(updateMock).toHaveBeenCalledWith(
            expect.objectContaining({
                request: expect.objectContaining({
                    agent_config: expect.objectContaining({
                        sandbox: expect.objectContaining({
                            enabled: true,
                            template: 'python-3.11',
                        }),
                    }),
                }),
            }),
            expect.any(Object),
        );
    });

    it('preserves memory config in the submit payload when saving an existing memory-enabled agent', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_MEMORY, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

        await waitFor(() => expect(updateMock).toHaveBeenCalled());

        expect(updateMock).toHaveBeenCalledWith(
            expect.objectContaining({
                request: expect.objectContaining({
                    agent_config: expect.objectContaining({
                        memory: {
                            enabled: true,
                            summarizer_model: 'claude-3-5-sonnet-latest',
                            max_entries: 2500,
                        },
                    }),
                }),
            }),
            expect.any(Object),
        );
    });

    it('omits sandbox from submit payload when sandbox is disabled', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /edit/i }));
        fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

        await waitFor(() => expect(updateMock).toHaveBeenCalled());

        expect(updateMock).toHaveBeenCalledWith(
            expect.objectContaining({
                request: expect.objectContaining({
                    agent_config: expect.not.objectContaining({
                        sandbox: expect.anything(),
                    }),
                }),
            }),
            expect.any(Object),
        );
    });

    it('renders agent detail in read-only mode by default', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(screen.getByText('research-agent')).toBeInTheDocument();
        expect(screen.getAllByText('active')).not.toHaveLength(0);
        expect(screen.getByText('Configuration')).toBeInTheDocument();
        expect(screen.getByText('Scheduling & Budget')).toBeInTheDocument();
        expect(screen.getByText('$0.50')).toBeInTheDocument();
        expect(screen.getByText('$5.00')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: /save changes/i })).not.toBeInTheDocument();
    });

    it('shows memory settings in overview and renders the memory tab without an icon', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_MEMORY, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(screen.getByText('Enabled')).toBeInTheDocument();
        expect(screen.getByText('claude-3-5-sonnet-latest')).toBeInTheDocument();
        expect(screen.getByText('2500')).toBeInTheDocument();

        const memoryTab = screen.getByTestId('agent-tab-memory');
        expect(within(memoryTab).getByText('Memory')).toBeInTheDocument();
        expect(memoryTab.querySelector('svg')).toBeNull();

        expect(screen.getByTestId('agent-memory-status-label')).toHaveClass('whitespace-nowrap');
    });

    it('shows concrete memory defaults in overview when optional values are unset', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_MEMORY_DEFAULTS, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(
            screen.getByText('Platform default (runtime-configured; fallback: claude-haiku-4-5)')
        ).toBeInTheDocument();
        expect(screen.getByText('10,000')).toBeInTheDocument();
    });

    it('uses customer-facing labels for context management in the read-only overview', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT_WITH_CONTEXT_MANAGEMENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(screen.getByText('Long-Running Task Context')).toBeInTheDocument();
        expect(screen.getByText('Summarizer Model')).toBeInTheDocument();
        expect(screen.getByText('gpt-4o-mini (OpenAI)')).toBeInTheDocument();
        expect(screen.getByText('Always Keep Outputs From')).toBeInTheDocument();
        expect(screen.getByText('Save Important Facts Before Summarizing')).toBeInTheDocument();
        expect(screen.getByText('web_search')).toBeInTheDocument();
        expect(screen.getByText('Enabled')).toBeInTheDocument();
    });

    it('shows loading state', () => {
        agentMock.mockReturnValue({ data: undefined, isLoading: true, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(screen.getByText('Loading agent...')).toBeInTheDocument();
    });

    it('shows error state for 404', () => {
        agentMock.mockReturnValue({ data: null, isLoading: false, error: new Error('Not found') });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(screen.getByText('Agent Not Found')).toBeInTheDocument();
        expect(screen.getByText('research-agent')).toBeInTheDocument();
    });

    it('switches to edit mode when Edit is clicked', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /edit/i }));

        expect(screen.getByRole('button', { name: /save changes/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
        expect(screen.getByDisplayValue('Research Agent')).toBeInTheDocument();
        expect(screen.getByDisplayValue('You are a research assistant.')).toBeInTheDocument();
    });

    it('shows the disabled agent warning in read-only mode', async () => {
        const disabledAgent = { ...MOCK_AGENT, status: 'disabled' as const };
        agentMock.mockReturnValue({ data: disabledAgent, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

        expect(screen.getAllByText('disabled')).not.toHaveLength(0);
        expect(screen.getByText('Disabled agents cannot be used for new task submissions.')).toBeInTheDocument();
    });

    describe('Supervisor topology — Deep Research read-only rendering (S10, invariant #2)', () => {
        it('shows preset + topology + Deep Research config read-only in the view mode', async () => {
            agentMock.mockReturnValue({ data: MOCK_AGENT_SUPERVISOR, isLoading: false, error: null });

            render(<AgentDetailPage />, { wrapper: createWrapper() });

            expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

            // Customer-facing preset + derived topology, both read-only (not <select>).
            const preset = screen.getByTestId('agent-config-preset');
            expect(preset).toHaveTextContent('Deep Research');
            expect(preset.tagName).not.toBe('SELECT');
            expect(screen.getByTestId('agent-config-topology')).toHaveTextContent('Deep Research');
            expect(
                screen.getByText(/fixed at agent creation; create a new agent to change it/i)
            ).toBeInTheDocument();

            // Deep Research config section renders with persisted values, disabled.
            const fanout = screen.getByTestId('supervisor-config-max-fanout') as HTMLInputElement;
            expect(fanout.value).toBe('7');
            expect(fanout).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-max-iterations')).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-writer-style')).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-scope-clarification')).toBeDisabled();
            expect(screen.getByText('web_search')).toBeInTheDocument();
            expect(screen.getByText('internal_docs')).toBeInTheDocument();
            // Read-only: no chip add-input.
            expect(screen.queryByPlaceholderText(/add source name/i)).not.toBeInTheDocument();
        });

        it('keeps preset/topology non-editable in EDIT mode (invariant #2 — no editable control)', async () => {
            agentMock.mockReturnValue({ data: MOCK_AGENT_SUPERVISOR, isLoading: false, error: null });

            render(<AgentDetailPage />, { wrapper: createWrapper() });

            expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

            fireEvent.click(screen.getByRole('button', { name: /edit/i }));
            expect(screen.getByRole('button', { name: /save changes/i })).toBeInTheDocument();

            // Preset/topology render as plain text, NOT an editable control.
            const preset = screen.getByTestId('agent-config-preset');
            expect(preset.tagName).not.toBe('SELECT');
            expect(preset.tagName).not.toBe('INPUT');
            expect(preset).toHaveTextContent('Deep Research');
            expect(screen.getByTestId('agent-config-topology')).toHaveTextContent('Deep Research');
            expect(
                screen.getByText(/fixed at agent creation; create a new agent to change it/i)
            ).toBeInTheDocument();

            // The Deep Research config section stays disabled on the edit form too.
            expect(screen.getByTestId('supervisor-config-max-fanout')).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-writer-style')).toBeDisabled();
        });

        it('does not include topology/preset/supervisor in the update payload (immutable — not re-sent)', async () => {
            agentMock.mockReturnValue({ data: MOCK_AGENT_SUPERVISOR, isLoading: false, error: null });

            render(<AgentDetailPage />, { wrapper: createWrapper() });

            expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

            fireEvent.click(screen.getByRole('button', { name: /edit/i }));
            fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

            await waitFor(() => expect(updateMock).toHaveBeenCalled());

            const payload = updateMock.mock.calls[0][0];
            expect(payload.request.agent_config.topology).toBeUndefined();
            expect(payload.request.agent_config.preset).toBeUndefined();
            expect(payload.request.agent_config.supervisor).toBeUndefined();
        });

        it('does not render the Deep Research config section for a react (non-supervisor) agent', async () => {
            agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

            render(<AgentDetailPage />, { wrapper: createWrapper() });

            expect(await screen.findByRole('heading', { name: 'Research Agent' })).toBeInTheDocument();

            expect(screen.queryByText('Deep Research Configuration')).not.toBeInTheDocument();
            expect(screen.queryByTestId('supervisor-config-max-fanout')).not.toBeInTheDocument();
            // The preset/topology fields still render read-only, showing the react defaults.
            expect(screen.getByTestId('agent-config-topology')).toHaveTextContent('ReAct');
        });
    });
});
