import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AgentsListPage } from './AgentsListPage';

const agentsMock = vi.fn();

vi.mock('./useAgents', () => ({
    useAgents: (status?: string) => agentsMock(status),
}));

vi.mock('./CreateAgentDialog', () => ({
    CreateAgentDialog: ({ open }: { open: boolean }) =>
        open ? <div data-testid="create-agent-dialog">Create Agent Dialog</div> : null,
}));

afterEach(() => {
    cleanup();
    agentsMock.mockReset();
});

const MOCK_AGENTS = [
    {
        agent_id: 'research-agent',
        display_name: 'Research Agent',
        provider: 'anthropic',
        model: 'claude-3-5-sonnet-latest',
        status: 'active' as const,
        created_at: '2026-03-27T18:00:00Z',
        updated_at: '2026-03-27T18:00:00Z',
    },
    {
        agent_id: 'support-agent',
        display_name: 'Support Agent',
        provider: 'openai',
        model: 'gpt-4o',
        status: 'disabled' as const,
        created_at: '2026-03-26T12:00:00Z',
        updated_at: '2026-03-26T12:00:00Z',
    },
];

describe('AgentsListPage', () => {
    it('renders agents list with mock data', () => {
        agentsMock.mockReturnValue({ data: MOCK_AGENTS, isLoading: false });

        render(
            <MemoryRouter>
                <AgentsListPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('Agents')).toBeInTheDocument();
        expect(screen.getByText('Research Agent')).toBeInTheDocument();
        expect(screen.getByText('Support Agent')).toBeInTheDocument();
        expect(screen.getByText('research-agent')).toBeInTheDocument();
        expect(screen.getByText('support-agent')).toBeInTheDocument();
        expect(screen.getByText('anthropic')).toBeInTheDocument();
        expect(screen.getByText('openai')).toBeInTheDocument();
        expect(screen.getByText('active')).toBeInTheDocument();
        expect(screen.getByText('disabled')).toBeInTheDocument();
    });

    it('shows empty state when no agents', () => {
        agentsMock.mockReturnValue({ data: [], isLoading: false });

        render(
            <MemoryRouter>
                <AgentsListPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('No agents found')).toBeInTheDocument();
        expect(screen.getByText('Create an agent to get started.')).toBeInTheDocument();
    });

    it('shows loading state', () => {
        agentsMock.mockReturnValue({ data: [], isLoading: true });

        render(
            <MemoryRouter>
                <AgentsListPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('Loading agents...')).toBeInTheDocument();
    });

    it('status filter calls useAgents with selected value', () => {
        agentsMock.mockReturnValue({ data: MOCK_AGENTS, isLoading: false });

        render(
            <MemoryRouter>
                <AgentsListPage />
            </MemoryRouter>,
        );

        const select = screen.getByDisplayValue('All');
        expect(select).toBeInTheDocument();

        fireEvent.change(select, { target: { value: 'active' } });

        // After selecting 'active', useAgents should have been called with 'active'
        expect(agentsMock).toHaveBeenCalledWith('active');
    });

    it('Create Agent button opens dialog', () => {
        agentsMock.mockReturnValue({ data: [], isLoading: false });

        render(
            <MemoryRouter>
                <AgentsListPage />
            </MemoryRouter>,
        );

        expect(screen.queryByTestId('create-agent-dialog')).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /create agent/i }));

        expect(screen.getByTestId('create-agent-dialog')).toBeInTheDocument();
    });

    it('renders agent display names as links to detail pages', () => {
        agentsMock.mockReturnValue({ data: MOCK_AGENTS, isLoading: false });

        render(
            <MemoryRouter>
                <AgentsListPage />
            </MemoryRouter>,
        );

        const link = screen.getByRole('link', { name: 'Research Agent' });
        expect(link).toHaveAttribute('href', '/agents/research-agent');
    });
});
