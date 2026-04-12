import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
});
