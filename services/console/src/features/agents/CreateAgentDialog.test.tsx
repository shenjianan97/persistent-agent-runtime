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
            { provider: 'openai', model_id: 'gpt-4o', display_name: 'GPT-4o' },
            { provider: 'openai', model_id: 'gpt-4o-mini', display_name: 'GPT-4o mini' },
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
    it('uses a max-height constrained flex layout so tall create forms stay fully visible and scroll internally', () => {
        render(<CreateAgentDialog open onOpenChange={() => {}} />, { wrapper: createWrapper() });

        const dialog = screen.getByRole('dialog');
        const form = dialog.querySelector('form');

        expect(dialog.className).toContain('max-h-[calc(100vh-2rem)]');
        expect(dialog.className).toContain('sm:max-h-[calc(100vh-4rem)]');
        expect(dialog.className).toContain('overflow-hidden');
        expect(dialog.className).toContain('flex');
        expect(dialog.className).toContain('flex-col');
        expect(form).not.toBeNull();
        expect(form?.className).toContain('flex');
        expect(form?.className).toContain('flex-col');
        expect(form?.className).toContain('min-h-0');
        
        const scrollArea = form?.querySelector('.overflow-y-auto');
        expect(scrollArea).not.toBeNull();
        expect(scrollArea?.className).toContain('flex-1');
    });

    it('renders context management controls and includes them in the create payload', async () => {
        render(<CreateAgentDialog open onOpenChange={() => {}} />, { wrapper: createWrapper() });

        fireEvent.change(screen.getByLabelText(/agent name/i), { target: { value: 'Context Agent' } });
        fireEvent.click(screen.getByText('Enable Memory'));
        fireEvent.change(screen.getByTestId('context-management-summarizer-model'), {
            target: { value: 'claude-3-5-haiku-latest' },
        });
        fireEvent.change(screen.getByPlaceholderText(/add tool name and press enter/i), {
            target: { value: 'web_search' },
        });
        fireEvent.keyDown(screen.getByPlaceholderText(/add tool name and press enter/i), { key: 'Enter' });
        fireEvent.click(screen.getByTestId('context-management-pre-tier3-flush'));

        fireEvent.click(screen.getByRole('button', { name: /create/i }));

        await waitFor(() => expect(createMock).toHaveBeenCalled());

        expect(screen.getByTestId('context-management-summarizer-model')).toBeInTheDocument();
        expect(createMock).toHaveBeenCalledWith(
            expect.objectContaining({
                agent_config: expect.objectContaining({
                    context_management: {
                        summarizer_model: 'claude-3-5-haiku-latest',
                        exclude_tools: ['web_search'],
                        pre_tier3_memory_flush: true,
                    },
                }),
            }),
            expect.any(Object),
        );
    });

    it('serializes pre_tier3_memory_flush explicitly when the user sets summarizer_model only (P1 fix)', async () => {
        render(<CreateAgentDialog open onOpenChange={() => {}} />, { wrapper: createWrapper() });

        fireEvent.change(screen.getByLabelText(/agent name/i), { target: { value: 'Partial Ctx Agent' } });
        fireEvent.change(screen.getByTestId('context-management-summarizer-model'), {
            target: { value: 'claude-3-5-haiku-latest' },
        });

        fireEvent.click(screen.getByRole('button', { name: /create/i }));

        await waitFor(() => expect(createMock).toHaveBeenCalled());

        const payload = createMock.mock.calls[0][0];
        expect(payload.agent_config.context_management).toEqual({
            summarizer_model: 'claude-3-5-haiku-latest',
            pre_tier3_memory_flush: false,
        });
    });

    it('filters summarizer-model options to the currently selected provider (P2 fix)', () => {
        render(<CreateAgentDialog open onOpenChange={() => {}} />, { wrapper: createWrapper() });

        const summarizerSelect = screen.getByTestId('context-management-summarizer-model') as HTMLSelectElement;
        const optionValues = Array.from(summarizerSelect.querySelectorAll('option')).map((o) => o.value);

        expect(optionValues).toContain('claude-3-5-sonnet-latest');
        expect(optionValues).toContain('claude-3-5-haiku-latest');
        expect(optionValues).not.toContain('gpt-4o');
        expect(optionValues).not.toContain('gpt-4o-mini');
    });

    it('includes memory config in the create payload when memory is enabled', async () => {
        render(<CreateAgentDialog open onOpenChange={() => {}} />, { wrapper: createWrapper() });

        fireEvent.change(screen.getByLabelText(/agent name/i), { target: { value: 'Memory Agent' } });
        fireEvent.click(screen.getByText('Enable Memory'));
        fireEvent.change(screen.getByLabelText(/max entries/i), { target: { value: '2500' } });
        fireEvent.change(screen.getByPlaceholderText(/claude-3-5-haiku-latest/i), {
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
