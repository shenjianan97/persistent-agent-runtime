import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CreateAgentDialog } from './CreateAgentDialog';

const createMock = vi.fn();

vi.mock('./useAgents', () => ({
    useCreateAgent: () => ({
        mutate: createMock,
        isPending: false,
    }),
}));

vi.mock('@/features/submit/useModels', () => ({
    useModels: () => ({
        data: [
            { provider: 'anthropic', model_id: 'claude-3-5-sonnet-latest', display_name: 'Claude 3.5 Sonnet' },
            { provider: 'anthropic', model_id: 'claude-3-5-haiku-latest', display_name: 'Claude 3.5 Haiku' },
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
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
}

afterEach(() => {
    cleanup();
    createMock.mockReset();
});

describe('CreateAgentDialog', () => {
    it('includes memory config in the create payload when memory is enabled', async () => {
        render(<CreateAgentDialog open onOpenChange={() => {}} />, { wrapper: createWrapper() });

        fireEvent.change(screen.getByLabelText(/agent name/i), { target: { value: 'Memory Agent' } });
        fireEvent.click(screen.getByText('Enable Memory'));
        fireEvent.change(screen.getByLabelText(/max entries/i), { target: { value: '2500' } });
        fireEvent.change(screen.getByLabelText(/summarizer model/i), {
            target: { value: 'claude-3-5-haiku-latest' },
        });

        fireEvent.click(screen.getByRole('button', { name: /create/i }));

        await waitFor(() => expect(createMock).toHaveBeenCalled());

        expect(createMock).toHaveBeenCalledWith(
            expect.objectContaining({
                agent_config: expect.objectContaining({
                    memory: {
                        enabled: true,
                        max_entries: 2500,
                        summarizer_model: 'claude-3-5-haiku-latest',
                    },
                }),
            }),
            expect.any(Object),
        );
    });
});
