import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
    created_at: '2026-03-27T18:00:00Z',
    updated_at: '2026-03-27T18:00:00Z',
};

describe('AgentDetailPage', () => {
    it('renders agent detail with all form fields', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        await waitFor(() => {
            expect(screen.getByText('Research Agent')).toBeInTheDocument();
        });

        expect(screen.getByText('research-agent')).toBeInTheDocument();
        expect(screen.getByText('active')).toBeInTheDocument();
        expect(screen.getByText('Agent Detail')).toBeInTheDocument();
        expect(screen.getByText('Configuration')).toBeInTheDocument();
        expect(screen.getByText('Lifecycle')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /save changes/i })).toBeInTheDocument();
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

    it('Submit Task CTA is present and enabled for active agent', async () => {
        agentMock.mockReturnValue({ data: MOCK_AGENT, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        await waitFor(() => {
            expect(screen.getByText('Research Agent')).toBeInTheDocument();
        });

        const submitButton = screen.getByRole('button', { name: /submit task/i });
        expect(submitButton).toBeInTheDocument();
        expect(submitButton).not.toBeDisabled();
    });

    it('Submit Task CTA is disabled when agent is disabled', async () => {
        const disabledAgent = { ...MOCK_AGENT, status: 'disabled' as const };
        agentMock.mockReturnValue({ data: disabledAgent, isLoading: false, error: null });

        render(<AgentDetailPage />, { wrapper: createWrapper() });

        await waitFor(() => {
            expect(screen.getByText('Research Agent')).toBeInTheDocument();
        });

        const submitButton = screen.getByRole('button', { name: /submit task/i });
        expect(submitButton).toBeDisabled();
    });
});
