import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToolServersListPage } from './ToolServersListPage';

const toolServersMock = vi.fn();

vi.mock('./useToolServers', () => ({
    useToolServers: (status?: string) => toolServersMock(status),
}));

vi.mock('./RegisterToolServerDialog', () => ({
    RegisterToolServerDialog: () => null,
}));

afterEach(() => {
    cleanup();
    toolServersMock.mockReset();
});

const MOCK_SERVERS = [
    {
        server_id: 'srv-1',
        name: 'Code Tools',
        url: 'https://tools.example.com/mcp',
        auth_type: 'bearer_token' as const,
        status: 'active' as const,
        created_at: '2026-04-01T10:00:00Z',
        updated_at: '2026-04-01T10:00:00Z',
    },
    {
        server_id: 'srv-2',
        name: 'Search Server',
        url: 'https://search.example.com/mcp',
        auth_type: 'none' as const,
        status: 'disabled' as const,
        created_at: '2026-04-02T12:00:00Z',
        updated_at: '2026-04-02T12:00:00Z',
    },
];

describe('ToolServersListPage', () => {
    it('renders tool servers list with mock data', () => {
        toolServersMock.mockReturnValue({ data: MOCK_SERVERS, isLoading: false });

        render(
            <MemoryRouter>
                <ToolServersListPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('Tool Servers')).toBeInTheDocument();
        expect(screen.getByText('Code Tools')).toBeInTheDocument();
        expect(screen.getByText('Search Server')).toBeInTheDocument();
        expect(screen.getByText('active')).toBeInTheDocument();
        expect(screen.getByText('disabled')).toBeInTheDocument();
    });

    it('shows loading state', () => {
        toolServersMock.mockReturnValue({ data: [], isLoading: true });

        render(
            <MemoryRouter>
                <ToolServersListPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('Loading tool servers...')).toBeInTheDocument();
    });

    it('shows empty state when no servers registered', () => {
        toolServersMock.mockReturnValue({ data: [], isLoading: false });

        render(
            <MemoryRouter>
                <ToolServersListPage />
            </MemoryRouter>,
        );

        expect(screen.getByText('No tool servers registered')).toBeInTheDocument();
        expect(screen.getByText('Register one to give your agents custom tools.')).toBeInTheDocument();
    });
});
